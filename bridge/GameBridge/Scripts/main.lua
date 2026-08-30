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
local function resolve(name, allow_scan)
    local hit = CACHE[name]
    if hit and valid(hit) and fullName(hit) == name then
        STATS.resolve_hits = STATS.resolve_hits + 1
        return hit
    end
    if hit then
        CACHE[name] = nil
        STATS.cache_evictions = STATS.cache_evictions + 1
    end

    -- Fallback: a linear sweep of the whole object array, on the game thread.
    --
    -- Object count is dominated by the engine and loaded assets rather than by
    -- world size: a one-corridor game carries 23,919 objects, and another
    -- title's *main menu* carries 29,784. Even with the FName comparison below
    -- that is ~0.8 us per object -- around 20 ms here, scaling linearly, and
    -- every millisecond of it a frame hitch.
    --
    -- Too expensive to spend implicitly, so it is off by default: the caller is
    -- told to run `find` first, which fills the cache from objects it is already
    -- holding and costs nothing extra. Pass allow_scan to opt in.
    if not allow_scan then
        return nil, "not cached; call find for this class first, or pass "
                 .. "allow_scan=1 to accept a full object-array sweep "
                 .. "(~0.8us/object on the game thread)"
    end

    STATS.resolve_scans = STATS.resolve_scans + 1
    local found
    pcall(function()
        local all = FindAllOf("Object")
        if not all then return end

        -- Compare FNames, not full names. GetFullName builds a whole path string
        -- per object; GetFName hands back the leaf, and unlike UObject, two
        -- FNames compare correctly with ==. Measured on 23,919 objects:
        -- GetFullName 1.76 us/obj, GetFName 0.81 us/obj -- 2.2x, before counting
        -- that the full name is now built only for the handful of leaf matches.
        -- The target FName is constructed once, outside the loop.
        -- (Advice from the UE4SS maintainer on RE-UE4SS#1402.)
        local leaf = name:match("([^%.:]+)$") or name
        local target = FName(leaf)

        for i = 1, #all do
            local o = all[i]
            local same
            pcall(function() same = (o:GetFName() == target) end)
            if same then
                -- Leaf matched; now it is worth paying for the full path, since
                -- distinct objects can share a leaf name across outers.
                if fullName(o) == name then found = o; return end
            end
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

-- Track the world by name so a change can be detected without reflection.
local LAST_WORLD = nil
local WORLD_CHANGES = 0

--- True when the object array should not be swept.
---
--- The obvious check -- reading UWorld::IsInSeamlessTravel -- does not work, and
--- fails in the way this project exists to catch. It is a plain C++ method, not a
--- UFUNCTION or UPROPERTY, so UE4SS reflection cannot see it; the earlier code
--- also read it as a property rather than calling it. Both mistakes resolve to
--- nil, `nil == true` is false, and pcall swallows the rest. The result was a
--- guard that could never fire, sitting in a repository whose whole argument is
--- that a check which cannot go red is not a check.
---
--- What replaces it is reflectable and observable: the world's full name, compared
--- against the previous tick. A changed name means a different world, which is the
--- thing the cache actually needs to know about. `world_changes` is reported by
--- ping so this one is not a silent claim either.
local function worldName()
    local w = UEHelpers.GetWorld()
    if not valid(w) then return nil end
    return fullName(w)
end

local function travelling()
    local n = worldName()
    if n == nil then return true end          -- no world: mid-transition or torn down
    if LAST_WORLD == nil then LAST_WORLD = n; return false end
    if n ~= LAST_WORLD then
        LAST_WORLD = n
        WORLD_CHANGES = WORLD_CHANGES + 1
        return true                            -- refuse this tick, cache is dropped
    end
    return false
end

--- Negative control for the travel guard.
---
--- The guard this replaced was dead for months without anyone noticing, so this
--- one ships with the means to prove it still fires: poison the tracked world
--- name, call the guard, require it to trip, then restore. If this ever returns
--- alive=false the guard has gone dead again and nothing it reports means
--- anything. Reported by ping, so it is checked on every status call rather than
--- only when someone remembers to.
local function travelSelfTest()
    local saved, savedN = LAST_WORLD, WORLD_CHANGES
    LAST_WORLD = "poison-not-a-world"
    local tripped = travelling()
    LAST_WORLD, WORLD_CHANGES = saved, savedN
    return tripped == true
end

--- Diagnostic: what the old reflection-based check actually yields. Kept so the
--- claim above is checkable rather than asserted.
local function travelProbe()
    local w = UEHelpers.GetWorld()
    if not valid(w) then return { world = false } end
    local ok, v = pcall(function() return w.IsInSeamlessTravel end)
    local okc, vc = pcall(function() return w:IsInSeamlessTravel() end)
    return {
        prop_pcall_ok = ok, prop_type = type(v), prop_is_true = (v == true),
        call_pcall_ok = okc, call_type = type(vc),
    }
end

-- ── ops ─────────────────────────────────────────────────────────────────────

local OPS = {}

function OPS.ping()
    local w = UEHelpers.GetWorld()
    return { ok = true, world = valid(w) and fullName(w) or nil, travelling = travelling(),
             world_changes = WORLD_CHANGES, travel_guard_alive = travelSelfTest(),
             travel_probe = travelProbe(),
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

--- Measure what a name lookup actually costs on this game.
---
--- Three phases, so the cost can be attributed rather than guessed at:
---   enum    -- FindAllOf(class) alone
---   control -- a per-object call that crosses the Lua binding and does almost
---              nothing (IsValid), isolating dispatch + loop overhead
---   name    -- the same loop calling GetFullName and comparing
--- name minus control is what the string work actually costs.
---
--- Timing note: os.clock() delegates to the CRT clock(), and MSVC's clock()
--- returns wall-clock time since process start, not CPU time -- it deliberately
--- does not follow ISO C here. CLOCKS_PER_SEC is 1000, so resolution is 1 ms and
--- every phase is averaged over several rounds to keep quantisation from
--- dominating a single reading.
function OPS.bench(c)
    if travelling() then return { ok = false, err = "world is travelling" } end

    local class  = c.class or "Object"
    local rounds = math.min(tonumber(c.rounds) or 5, 20)

    local function timed(fn)
        local worst, total = 0, 0
        for _ = 1, rounds do
            local t = os.clock()
            pcall(fn)
            local dt = os.clock() - t
            total = total + dt
            if dt > worst then worst = dt end
        end
        return total / rounds, worst
    end

    local all, n = nil, 0
    local enum_avg, enum_worst = timed(function()
        all = FindAllOf(class)
        n = all and #all or 0
    end)

    if n == 0 then
        return { ok = true, class = class, objects = 0,
                 note = "no instances of that class" }
    end

    local ctrl_avg = timed(function()
        for i = 1, n do
            local v = all[i]
            if v and v:IsValid() and false then break end
        end
    end)

    local name_avg, name_worst = timed(function()
        for i = 1, n do
            if fullName(all[i]) == "no-such-object" then break end
        end
    end)

    -- The maintainer's suggested alternative (UE4SS-RE/RE-UE4SS#1402): compare
    -- FNames rather than building a full path string per object, with the target
    -- FName constructed once outside the loop.
    local fname_avg, fname_worst, fname_ok = 0, 0, false
    local probe = { eq_works = nil, ctor_ok = nil, getf_ok = nil }
    pcall(function()
        local target = FName("no-such-object")
        probe.ctor_ok = target ~= nil
        local one
        pcall(function() one = all[1]:GetFName() end)
        probe.getf_ok = one ~= nil
        -- Does == on two FNames actually compare, or is it wrapper identity like
        -- UObject? Worth knowing before trusting a loop built on it.
        pcall(function() probe.eq_works = (all[1]:GetFName() == one) end)
        fname_avg, fname_worst = timed(function()
            for i = 1, n do
                if all[i]:GetFName() == target then break end
            end
        end)
        fname_ok = true
    end)

    local function ms(x) return math.floor(x * 100000 + 0.5) / 100 end
    local function us(x) return math.floor((x / n) * 100000000 + 0.5) / 100 end

    return {
        ok = true,
        class = class,
        objects = n,
        rounds = rounds,
        enum_ms = ms(enum_avg),
        enum_worst_ms = ms(enum_worst),
        control_ms = ms(ctrl_avg),
        name_ms = ms(name_avg),
        name_worst_ms = ms(name_worst),
        -- the part attributable to GetFullName itself, binding overhead removed
        name_minus_control_ms = ms(name_avg - ctrl_avg),
        us_per_obj_control = us(ctrl_avg),
        us_per_obj_name = us(name_avg),
        us_per_obj_delta = us(name_avg - ctrl_avg),
        fname_ms = fname_ok and ms(fname_avg) or nil,
        fname_worst_ms = fname_ok and ms(fname_worst) or nil,
        us_per_obj_fname = fname_ok and us(fname_avg) or nil,
        fname_probe = probe,
        cached = cacheSize(),
    }
end

function OPS.read(c)
    local o, why = resolve(c.obj, c.allow_scan)
    if not valid(o) then
        return { ok = false, err = why or "object not found" }
    end
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
    local o, why = resolve(c.obj, c.allow_scan)
    if not valid(o) then
        return { ok = false, err = why or "object not found" }
    end

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
    local o, why = resolve(c.obj, c.allow_scan)
    if not valid(o) then
        return { ok = false, err = why or "object not found" }
    end
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

    -- Read whole, then split, so a half-written line is not consumed. f:lines()
    -- yields a trailing partial line as if it were complete; the cursor would
    -- advance past it and the command would be skipped forever once the writer
    -- finished it, showing up only as an unexplained timeout on the other side.
    local blob = f:read("a") or ""
    f:close()
    local lines = {}
    for line in blob:gmatch("([^\n]*)\n") do lines[#lines + 1] = line end

    local n = 0
    for _, line in ipairs(lines) do
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
