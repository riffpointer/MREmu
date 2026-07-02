# MREmu Codebase Analysis

> **Project**: MREmu — ARM/Thumb emulator for MediaTek MRE (VXP) platform apps
> **Stack**: C++17, Unicorn Engine, Capstone, SFML, ImGui, libADLMIDI, ELFIO
> **Analysis date**: 2026-06-29

---

## P0 — Critical Bugs (crash / data corruption / security)

---

### P0-1 · `abort` called without parentheses — stack allocation silently never aborts

**File**: `Cpu.cpp:174`

```cpp
stack_p = Memory::shared_malloc(stack_size);
if (stack_p == 0)
    abort;   // missing () — this is a no-op!
```

`abort` is a function pointer, not a call. If `shared_malloc` returns `NULL` (OOM), execution
continues with `stack_p == 0`. `write_reg` then writes `0 + stack_size` (128 KB) as the stack
pointer — a valid but **wrong** emulator address, causing silent stack corruption that will
crash or produce UB later.

**Fix**: `abort();`

---

### P0-2 · GDB buffer overflow in `put_to_out` — fixed 8 MB buffers with no bounds check

**File**: `GDB.cpp:64-68`

```cpp
const uint32_t outbuf_size = 8 * 1024 * 1024;
uint8_t outbuf[outbuf_size];
uint32_t outbuf_pos = 0;

void put_to_out(const uint8_t* data, uint32_t size) {
    memcpy(outbuf + outbuf_pos, data, size);   // no bounds check
    outbuf_pos += size;
}
```

Both `inbuf` and `outbuf` lack any overflow guard. The `process_vFile_pread` path calls
`make_answer(tmp + hex_data)` where `hex_data` length is controlled by the GDB client's
`count` field — completely attacker-controlled — meaning a crafted packet can write past
the end of `outbuf`.

**Fix**: Guard `put_to_out` with a size check before `memcpy`; use a dynamic buffer.

---

### P0-3 · `process_z` — `std::map::at` throws on missing breakpoint address

**File**: `GDB.cpp:244`

```cpp
if(!hooks.at(addr))      // throws std::out_of_range if addr not in map
    return make_answer("");
```

Sending a GDB `z0,<addr>,1` packet for an address that was never registered terminates the
emulator with an unhandled exception. Use `hooks.find()` instead of `at()`.

**Fix**:
```cpp
auto it = hooks.find(addr);
if (it == hooks.end()) return make_answer("");
uc_hook_del(uc, it->second);
hooks.erase(it);
```

---

### P0-4 · `Memory::realloc` returns freed (dangling) address when `size == 0`

**File**: `Memory.cpp:135-138`

```cpp
if (size == 0) {
    free(addr);
    return addr;   // dangling pointer returned to caller!
}
```

Callers receive a pointer to freed memory, leading to use-after-free UB.

**Fix**: `return 0;` after `free(addr)`.

---

### P0-5 · ELF relocation reads using virtual address as a file offset — OOB heap read

**File**: `ArmApp.cpp:108`

```cpp
ELFIO::Elf32_Rel* sym = (ELFIO::Elf32_Rel*)&file_context[psec->get_address()]; // BUG
```

`get_address()` is the **virtual address**, not the file offset (`get_offset()`). For
position-independent ELF binaries the virtual address is typically much larger than the
file size, so this indexes far outside `file_context`, causing heap corruption / UB.

**Fix**: Use `psec->get_offset()` and validate bounds.

---

### P0-6 · `bridge_hook` off-by-one — `>` should be `>=`

**File**: `Bridge.cpp:1116`

```cpp
if (ind > func_map.size())   // should be >=
    abort();
func_map[ind].f(uc);         // OOB when ind == func_map.size()
```

The idle trampoline is at `func_count * 2`, so `ind` can legitimately equal
`func_map.size()`, bypassing the guard and invoking an out-of-bounds lambda.

**Fix**: `if (ind >= (int)func_map.size()) abort();`

---

### P0-7 · Debug binary dump always active — unconditionally writes decrypted app to disk

**File**: `ArmApp.cpp:167-173`

```cpp
{//temp
    std::ofstream out("unpack.bin", std::ios::binary);
    if (out.is_open()) {
        out.write((char*)mem_location, segments_size);
```

Every VXP load overwrites `./unpack.bin` with the full decrypted binary. This is leftover
debug code that is a privacy leak and will silently corrupt partially-written files on
filesystem errors.

**Fix**: Remove or gate behind `#ifdef DEBUG` / a CLI flag.

---

## P1 — High-severity Bugs (wrong behavior, hangs, resource leaks)

---

### P1-1 · Emulation error spins in `while(1)` — process hangs forever

**File**: `Bridge.cpp:1187-1193`

```cpp
if (err) {
    printf("uc_emu_start returned %d ...\n", ...);
    while (1) { std::this_thread::sleep_for(milliseconds(1000)); }
}
```

Any unicorn error permanently hangs the main thread. The GUI freezes and the only recovery
is a force-kill.

**Fix**: Signal an error state and return; display an error in the UI instead.

---

### P1-2 · `vm_graphic_get_img_property_FIX` — single static buffer, not reentrant, NULL on OOM

**File**: `Graphic.cpp:497`

```cpp
static struct frame_prop* info = (frame_prop*)Memory::shared_malloc(sizeof(frame_prop));
```

Allocated once and reused every call. Any concurrent or nested call silently overwrites
the buffer. If `shared_malloc` fails at init time, `info` is permanently NULL and every
call will dereference it.

**Fix**: Allocate per-call; add NULL guard.

---

### P1-3 · `vm_sim_get_prefer_sim_card` copy-paste bug — calls wrong function

**File**: `Bridge.cpp:369-371`

```cpp
{FUNCN(vm_sim_get_prefer_sim_card), [](uc_engine* uc) {
    write_ret(uc, vm_sim_max_card_count());   // should call vm_sim_get_prefer_sim_card()
}},
```

Multi-SIM apps receive the max card count instead of the preferred SIM index.

---

### P1-4 · `vm_ucs2_to_gb2312` maps to ASCII converter — breaks CJK text

**File**: `Bridge.cpp:772-779`

```cpp
{FUNCN(vm_ucs2_to_gb2312), [](uc_engine* uc) {
    write_ret(uc, vm_ucs2_to_ascii(...));   // wrong conversion!
}},
```

Chinese-language apps will receive ASCII-truncated output from every `vm_ucs2_to_gb2312`
call, breaking text rendering and string processing for all non-ASCII characters.

---

### P1-5 · File handle leak — no check for valid mode flags in `vm_file_open`

**File**: `IO.cpp:157-166`

If no recognised mode flag is set, `fmode` stays as `std::ios::binary` only. The file can
open successfully (returning a valid handle) while silently performing no reads or writes.
The handle leaks on app exit if the destructor doesn't clean up.

---

### P1-6 · `hook_read/write_unmapped` auto-maps arbitrary pages with full permissions

**File**: `Cpu.cpp:143-155`

```cpp
static bool hook_read_unmapped(...) {
    uc_mem_map(uc, (address / 0x1000) * 0x1000, 0x1000, UC_PROT_ALL);
    return true;
}
```

Any guest fault — including from buggy or adversarial VXP code — silently maps a new
page with full RWX permissions. This defeats Unicorn's memory isolation entirely.

**Fix**: Return `false` to signal a fault to the guest, or log and abort.

---

### P1-7 · Unknown GDB byte not consumed — permanently stalls input processing

**File**: `GDB.cpp:584-592`

The `default` branch leaves `packet_len = 0`. `process_input` returns `false` but the
byte stays at the head of `inbuf`, blocking all future packets.

**Fix**: Set `packet_len = 1` in the `default` branch to consume the unknown byte.

---

## P2 — Medium Severity (wrong behavior / potential crashes / maintainability)

---

### P2-1 · `uint64_t shared_memory_offset` initialised with `NULL` — type mismatch

**File**: `Memory.cpp:17`

```cpp
uint64_t shared_memory_offset = NULL;   // NULL is (void*)0
```

**Fix**: Use `0ULL`.

---

### P2-2 · `Memory::malloc` size check can underflow — unsigned wrap-around

**File**: `Memory.cpp:100`

```cpp
if (size > free_memory_size - (allow_protected ? 0 : protected_size))
```

When `free_memory_size < protected_size` the subtraction wraps to a huge value, making
the check always pass and causing an over-commit allocation.

**Fix**:
```cpp
size_t usable = (allow_protected || free_memory_size <= protected_size)
                ? free_memory_size
                : free_memory_size - protected_size;
if (size > usable) return 0;
```

---

### P2-3 · `imgui_REG` calls `system("cls")` — Windows-only, security anti-pattern

**File**: `Cpu.cpp:77`

```cpp
system("cls");   // spawns a shell; no-op or worse on Linux/macOS
```

**Fix**: Use `printf("\033[2J\033[H")` for ANSI-compatible clear.

---

### P2-4 · Path regex uses Cyrillic `с/С` as range endpoints instead of ASCII `c/C`

**File**: `IO.cpp:30`

```cpp
std::regex pr("^[a-сA-С]:...");  // 'с' = U+0441, 'С' = U+0421 (Cyrillic!)
```

The range `[a-с]` is far wider than intended (`a-c`), accepting many unexpected characters.

**Fix**: Replace with `[a-cA-C]` (ASCII letters only).

---

### P2-5 · `find_packer` double-translates an already-host path through `path_from_emu`

**File**: `IO.cpp:376`

```cpp
info->size = fs::file_size(path_from_emu(el));  // el is already a host path
```

`path_from_emu` prepends `./fs/` a second time, producing a nonexistent path. `file_size`
throws or returns garbage.

**Fix**: `info->size = fs::file_size(el);`

---

### P2-6 · `vm_graphic_blt_ex` ignores `alpha` parameter — unimplemented blending

**File**: `Graphic.cpp:609`

```cpp
//TODO alpha blend
```

Alpha-blended sprites render fully opaque. Apps using transparency will look wrong.

---

### P2-7 · `process_P` (GDB write-register) no bounds check on register ID

**File**: `GDB.cpp:198`

```cpp
cpu_reg reg_info = cpu_reg_list[id];   // cpu_reg_list has 17 entries; id unchecked
```

A GDB client sending `id >= 17` causes an out-of-bounds array access.

**Fix**: `if (id >= 17) return make_answer("E01");`

---

### P2-8 · `vm_graphic_rotate` self-documented as incorrect

**File**: `Bridge.cpp:506`

```cpp
{FUNCN(vm_graphic_rotate), [](uc_engine* uc) { //WRONG
```

Apps using sprite rotation produce visually wrong output.

---

### P2-9 · TCP disconnect event (`VM_TCP_EVT_PIPE_BROKEN`) never delivered to apps

**File**: `Sock.cpp:34-52`

The disconnect-notification block is commented out. Networked apps cannot detect drops.

---

### P2-10 · `vm_tcp_connect` is synchronous — blocks the main thread during connect

**File**: `Sock.cpp:93-95`

```cpp
tcp.soc->setBlocking(true);
auto res = tcp.soc->connect(host, port);   // blocks entire emulator
tcp.soc->setBlocking(false);
```

The MRE API is async; the connect should return immediately and fire a callback.

---

## P3 — Low Severity / Code Quality

---

### P3-1 · 16 MB static GDB buffers always allocated — even without GDB mode

**File**: `GDB.cpp:22-29`

Both `inbuf` (8 MB) and `outbuf` (8 MB) are always present in BSS.

---

### P3-2 · `ARModuleBin.h` is 460 KB — should not be a header

**File**: `MREmu/ARModuleBin.h`

Inline binary array in a header forces recompilation of all including TUs.

---

### P3-3 · `unifont.h` is 12.8 MB — single largest compile-time bottleneck

**File**: `MREmu/MREngine/unifont.h`

Move the data to a `.cpp` file as `extern const` or load from disk at runtime.

---

### P3-4 · `Memory::malloc` inserts into sorted vector — O(n) per allocation

**File**: `Memory.cpp:110`

```cpp
regions.insert(regions.begin() + i, { new_adr, size });
```

Vector insert shifts all subsequent elements — O(n²) overall for many allocations.

---

### P3-5 · `vm_graphic_flush_layer` — O(W×H×L) triple nested loop per frame

**File**: `Graphic.cpp:300-321`

For a 240×320 screen with 2 layers: ~154 k iterations per frame. Use dirty-rect compositing
or `sf::RenderTexture` for GPU-accelerated blending.

---

### P3-6 · `buf_to_texture` — creates full `sf::Image` CPU copy every frame

**File**: `Graphic.cpp:11-31`

Use `sf::Texture::update(const Uint8*)` directly to avoid the intermediate allocation.

---

### P3-7 · `vm_graphic_fill_rect` — per-pixel conditional instead of `memset`

**File**: `Graphic.cpp:935-940`

The interior fill checks edge conditions for every pixel. Use `std::fill` / `memset` for
the interior and 4 line-fill passes for the border.

---

### P3-8 · `run_cpu` throws bare `int` with no catch site

**File**: `Bridge.cpp:1174-1175`

```cpp
if (n > 4)
    throw 1;
```

No `catch` exists anywhere in the codebase; this terminates via `std::terminate`.

**Fix**: Use `assert(n <= 4)` or a proper exception type.

---

### P3-9 · ADS data base placed 0x100 bytes past the end of allocated memory

**File**: `ArmApp.cpp:187`

```cpp
Bridge::ads_start(entry_point, vm_get_sym_entry_p, offset_mem + mem_size + 0x100);
```

`offset_mem + mem_size` is already one past the allocation; `+ 0x100` extends further into
adjacent or unmapped memory.

---

### P3-10 · Creating a third graphics layer calls `abort()` instead of returning error

**File**: `Graphic.cpp:163-164`

```cpp
else
    abort();
```

MRE supports up to 4 layers on some devices. Any app that creates 3+ layers crashes the
emulator instead of receiving `VM_GDI_FAILED`.

---

## Summary Table

| ID | Severity | File | Short Description |
|----|----------|------|-------------------|
| P0-1 | **Critical** | Cpu.cpp:174 | `abort` missing `()` — OOM on stack alloc silently ignored |
| P0-2 | **Critical** | GDB.cpp:67 | `put_to_out` no bounds check — attacker-controlled overflow |
| P0-3 | **Critical** | GDB.cpp:244 | `map::at` throws on missing bp — uncaught exception |
| P0-4 | **Critical** | Memory.cpp:137 | `realloc(p,0)` returns dangling pointer |
| P0-5 | **Critical** | ArmApp.cpp:108 | ELF reloc uses virtual addr as file offset — OOB heap read |
| P0-6 | **Critical** | Bridge.cpp:1116 | `bridge_hook` off-by-one — OOB `func_map` access |
| P0-7 | **Critical** | ArmApp.cpp:167 | Debug dump always writes decrypted binary to disk |
| P1-1 | **High** | Bridge.cpp:1190 | Emulation error → infinite sleep loop, process hangs |
| P1-2 | **High** | Graphic.cpp:497 | Static buffer in `get_img_property` — not reentrant, NULL risk |
| P1-3 | **High** | Bridge.cpp:370 | `vm_sim_get_prefer_sim_card` calls wrong function |
| P1-4 | **High** | Bridge.cpp:774 | `vm_ucs2_to_gb2312` mapped to ASCII — breaks CJK text |
| P1-5 | **High** | IO.cpp:157 | Missing mode flag check — silent no-op file handle |
| P1-6 | **High** | Cpu.cpp:146 | Unmapped page auto-maps full-RWX — no guest isolation |
| P1-7 | **High** | GDB.cpp:586 | Unknown GDB byte not consumed — stalls input permanently |
| P2-1 | Medium | Memory.cpp:17 | `uint64_t` assigned `NULL` pointer constant |
| P2-2 | Medium | Memory.cpp:100 | Malloc size check can underflow for protected regions |
| P2-3 | Medium | Cpu.cpp:77 | `system("cls")` — Windows-only, security anti-pattern |
| P2-4 | Medium | IO.cpp:30 | Regex uses Cyrillic `с/С` instead of ASCII `c/C` |
| P2-5 | Medium | IO.cpp:376 | `find_packer` double-translates already-host path |
| P2-6 | Medium | Graphic.cpp:609 | Alpha blending unimplemented in `blt_ex` |
| P2-7 | Medium | GDB.cpp:198 | `process_P` — OOB `cpu_reg_list` access on large id |
| P2-8 | Medium | Bridge.cpp:506 | `vm_graphic_rotate` self-documented as wrong |
| P2-9 | Medium | Sock.cpp:34 | TCP disconnect event never fired to guest |
| P2-10 | Medium | Sock.cpp:93 | `vm_tcp_connect` blocks main thread synchronously |
| P3-1 | Low | GDB.cpp:22 | 16 MB static buffers even without GDB mode |
| P3-2 | Low | ARModuleBin.h | 460 KB binary blob in a header — slow builds |
| P3-3 | Low | unifont.h | 12.8 MB font data in a header — massive build cost |
| P3-4 | Low | Memory.cpp:110 | O(n) vector insert in allocator hot path |
| P3-5 | Low | Graphic.cpp:300 | Triple-nested flush loop — O(W×H×L) per frame |
| P3-6 | Low | Graphic.cpp:11 | Full `sf::Image` CPU copy every frame update |
| P3-7 | Low | Graphic.cpp:935 | `fill_rect` per-pixel conditional instead of memset |
| P3-8 | Low | Bridge.cpp:1174 | `throw 1` — no catch site, terminates via `std::terminate` |
| P3-9 | Low | ArmApp.cpp:187 | ADS data base placed past end of allocated region |
| P3-10 | Low | Graphic.cpp:163 | 3rd layer creation calls `abort()` instead of returning error |
