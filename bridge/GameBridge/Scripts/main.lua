--[[
  GameBridge -- expose a running Unreal Engine game to an external process.

  Transport is a pair of append-only files, which is the pattern that already
  survives contact with shipped games: no sockets to punch through, no ordering
  ambiguity, and a full transcript on disk when something goes wrong.

      cmd.jsonl    external -> game   one JSON object per line
      resp.jsonl   game -> external   one JSON object per line, keyed by id

  A line cursor is kept in memory rather than deleting consumed commands, so
  there is no write/delete race with the writer.

  Commands:
      {"id":1,"op":"ping"}
      {"id":2,"op":"find","class":"BP_PlayerCharacter_C","limit":10}
      {"id":3,"op":"read","obj":"<full name>","prop":"Health"}
      {"id":4,"op":"write","obj":"<full name>","prop":"Health","value":250}
      {"id":5,"op":"call","obj":"<full name>","fn":"K2_GetActorLocation"}

  Deliberate constraints, each one paid for:
    * every command runs inside pcall; a bad request must not take the loop down
    * file names are pure ASCII -- Lua io.open goes through the system ANSI
      codepage and a non-ASCII path silently fails to open
    * no full FindAllOf sweep while the world is travelling; the object array is
      in flux and you get half-torn objects or nothing
    * UObject identity is by full name string, never by == on the objects
]]

local UEHelpers = require("UEHelpers")

local MOD_ROOT = debug.getinfo(1, "S").source:match("@?(.*[/\\])")
local DATA_DIR = MOD_ROOT and (MOD_ROOT .. "../data/") or "./GameBridge_data/"

local CFG = {
    tick_ms   = 200,
    cmd_file  = DATA_DIR .. "cmd.jsonl",
    resp_file = DATA_DIR .. "resp.jsonl",
    max_find  = 200,
}

-- ── json out ────────────────────────────────────────────────────────────────

local function esc(s)
    return (tostring(s):gsub("\\", "\\\\"):gsub('"', '\\"')
                       :gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t"))
end

local function jval(v)
    local t = type(v)
    if v == nil then return "null" end
    if t == "boolean" then return v and "true" or "false" end
    if t == "number" then
        -- NaN and infinities are not JSON; emitting them produces a document the
        -- other side cannot parse, which reads as "the bridge died".
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        return tostring(v)
    end
    if t == "table" then
        local parts = {}
        -- An empty Lua table is ambiguous: {} and [] are the same value. Tables
        -- meant as arrays are tagged so an empty list does not serialise as an
        -- empty object and break the reader's indexing.
        if v.__arr then
            for i = 1, #v do parts[#parts + 1] = jval(v[i]) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local n = #v
        if n > 0 then
            for i = 1, n do parts[#parts + 1] = jval(v[i]) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        for k, val in pairs(v) do
            if k ~= "__arr" then
                parts[#parts + 1] = '"' .. esc(k) .. '":' .. jval(val)
            end
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return '"' .. esc(v) .. '"'
end

local function respond(tbl)
    local f = io.open(CFG.resp_file, "a")
    if not f then return end
    f:write(jval(tbl), "\n")
    f:close()
end

-- ── minimal json in (flat objects only, which is all the protocol uses) ──────

local function parseLine(line)
    local o = {}
    for k, v in line:gmatch('"([%w_]+)"%s*:%s*"([^"]*)"') do o[k] = v end
    for k, v in line:gmatch('"([%w_]+)"%s*:%s*(-?%d+%.?%d*)') do o[k] = tonumber(v) end
    for k, v in line:gmatch('"([%w_]+)"%s*:%s*(true)') do o[k] = true end
    for k, v in line:gmatch('"([%w_]+)"%s*:%s*(false)') do o[k] = false end
    return o
end

-- ── helpers ─────────────────────────────────────────────────────────────────

local function valid(o)
    if not o then return false end
    local ok, r = pcall(function() return o:IsValid() end)
    return ok and r == true
end

local function fullName(o)
    local n
    pcall(function() n = o:GetFullName() end)
    return n
end

-- Full name -> object. Populated by `find`, which already holds the objects, so
-- the normal find-then-act flow never pays for a scan.
local CACHE = {}
local STATS = { resolve_hits = 0, resolve_scans = 0, cache_evictions = 0 }

local function cachePut(o)
    local n = fullName(o)
    if n then CACHE[n] = o end
    return n
end

--- Resolve an object by its full name.
---
--- Identity is the name string. UObject values cannot be compared with == in
--- UE4SS Lua, so any lookup matching by object reference silently finds nothing.
---
--- A cached entry is re-validated on every use: the engine recycles object slots,
--- so a pointer that is still "valid" can belong to something else entirely by
--- the time you use it. Checking the name again is what makes the cache safe.
local function resolve(name)
    local hit = CACHE[name]
    if hit and valid(hit) and fullName(hit) == name then
        STATS.resolve_hits = STATS.resolve_hits + 1
        return hit
    end
    if hit then
        CACHE[name] = nil
        STATS.cache_evictions = STATS.cache_evictions + 1
    end

    -- Fallback: a linear sweep of the whole object array. Fine on a small level,
    -- expensive on a large one, which is why the cache exists.
    STATS.resolve_scans = STATS.resolve_scans + 1
    local found
    pcall(function()
        local all = FindAllOf("Object")
        if not all then return end
        for i = 1, #all do
            if fullName(all[i]) == name then found = all[i]; return end
        end
    end)
    if found then cachePut(found) end
    return found
end

--- Drop everything. Objects do not survive a level change, and a stale entry
--- that happens to re-validate would hand back an object from the old world.
local function cacheClear()
    CACHE = {}
end

local function cacheSize()
    local n = 0
    for _ in pairs(CACHE) do n = n + 1 end
    return n
end

--- True while the world is mid-travel. Sweeping the object array here returns
--- garbage; better to refuse the command than to answer with nonsense.
local function travelling()
    local w = UEHelpers.GetWorld()
    if not valid(w) then return true end
    local t = false
    pcall(function() t = w.IsInSeamlessTravel == true end)
    return t
end

-- ── ops ─────────────────────────────────────────────────────────────────────

local OPS = {}

function OPS.ping()
    local w = UEHelpers.GetWorld()
    return { ok = true, world = valid(w) and fullName(w) or nil, travelling = travelling(),
             cached = cacheSize(), resolve_hits = STATS.resolve_hits,
             resolve_scans = STATS.resolve_scans, evictions = STATS.cache_evictions }
end

function OPS.find(c)
    if travelling() then return { ok = false, err = "world is travelling" } end
    local out, n = { __arr = true }, 0
    local limit = math.min(tonumber(c.limit) or 25, CFG.max_find)
    pcall(function()
        local all = FindAllOf(c.class)
        if not all then return end
        for i = 1, #all do
            if n >= limit then break end
            if valid(all[i]) then
                -- Cache on the way past. The caller is about to act on one of
                -- these, and we are already holding it.
                local nm = cachePut(all[i])
                if nm then n = n + 1; out[n] = nm end
            end
        end
    end)
    return { ok = true, count = n, objects = out }
end

function OPS.read(c)
    local o = resolve(c.obj)
    if not valid(o) then return { ok = false, err = "object not found" } end
    local v, got
    local ok = pcall(function() v = o[c.prop]; got = true end)
    if not ok or not got then return { ok = false, err = "property read failed" } end
    local t = type(v)
    if t == "userdata" then
        local s; pcall(function() s = v:ToString() end)
        return { ok = true, value = s or "<userdata>", vtype = "object" }
    end
    return { ok = true, value = v, vtype = t }
end

--- Write a property.
---
--- Returns only what it OBSERVED, never a bare success. The caller gets the
--- value read back immediately after the write, and decides for itself whether
--- that constitutes success. A bridge that reports "ok" and nothing else is a
--- bridge that can lie without being caught, and that is the exact failure this
--- whole design targets.
function OPS.write(c)
    local o = resolve(c.obj)
    if not valid(o) then return { ok = false, err = "object not found" } end

    local before
    pcall(function() before = o[c.prop] end)

    local wrote = pcall(function() o[c.prop] = c.value end)

    local after
    pcall(function() after = o[c.prop] end)

    return {
        ok = wrote,
        wrote = wrote,
        before = type(before) == "userdata" and "<object>" or before,
        after  = type(after) == "userdata" and "<object>" or after,
        requested = c.value,
    }
end

function OPS.call(c)
    local o = resolve(c.obj)
    if not valid(o) then return { ok = false, err = "object not found" } end
    local ret, called
    local ok = pcall(function() ret = o[c.fn](o); called = true end)
    if not ok or not called then return { ok = false, err = "call failed" } end
    return { ok = true, ret = type(ret) == "userdata" and "<object>" or ret }
end

-- ── loop ────────────────────────────────────────────────────────────────────

local cursor, tick, errors = 0, 0, 0

--- Skip everything already in the command file at load time.
---
--- Without this the cursor starts at 0 and every command left over from a
--- previous session is replayed on boot -- "whoever starts the process executes
--- the leftovers." A stale `write` silently changes state minutes after anyone
--- asked for it, and the symptom looks nothing like its cause.
local function purgeStale()
    local n = 0
    local f = io.open(CFG.cmd_file, "r")
    if f then
        for _ in f:lines() do n = n + 1 end
        f:close()
    end
    cursor = n
    return n
end

local function pump()
    tick = tick + 1

    -- Objects do not survive a level change; a stale entry that happened to
    -- re-validate would hand back an object belonging to the old world.
    if travelling() then cacheClear() end
    local f = io.open(CFG.cmd_file, "r")
    if not f then return end

    local n = 0
    for line in f:lines() do
        n = n + 1
        if n > cursor and #line > 2 then
            cursor = n
            local c = parseLine(line)
            local op = OPS[c.op or ""]
            local res
            if not op then
                res = { ok = false, err = "unknown op: " .. tostring(c.op) }
            else
                local ok, r = pcall(op, c)
                res = ok and r or { ok = false, err = tostring(r) }
            end
            res.id = c.id
            res.tick = tick
            respond(res)
        end
    end
    if n < cursor then cursor = n end   -- writer truncated the file; resync
    f:close()
end

LoopInGameThreadWithDelay(CFG.tick_ms, function()
    local ok, err = pcall(pump)
    if not ok then
        errors = errors + 1
        respond({ id = -1, ok = false, err = tostring(err), loop_errors = errors })
    end
end)

local stale = purgeStale()
respond({ id = 0, ok = true, event = "bridge_up", data_dir = DATA_DIR, purged_stale = stale })
print("[GameBridge] up, polling " .. CFG.cmd_file .. " (skipped " .. stale .. " stale)\n")
