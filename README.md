# fprime-dict-to-cpp

Generate an F Prime component that **proxies another deployment's dictionary**.

Point it at a deployment's JSON dictionary and it emits a component that speaks that
deployment's ground interface on your behalf:

* every one of the remote's **commands** becomes a command of the proxy, whose handler
  serializes the remote's opcode and arguments into an outgoing `Fw.Com` command packet;
* the remote's **events** and **telemetry channels** are re-declared on the proxy, and
  its inbound event, channelized-telemetry and packetized-telemetry packets are decoded
  and re-emitted through them;
* the types those members reference are re-declared too, so the result builds in a
  project that has never seen the remote's FPP.

Because the members are really declared, the target deployment's own ground dictionary
gains an entry for each of them. The ground system decodes the relayed traffic with no
knowledge that it came from somewhere else — which is the difference between this and
tunnelling opaque packets through something like `Svc::GenericHub`.

```sh
fprime-dict-to-cpp RefTopologyDictionary.json -o Ref/RefProxy
```

```
RefProxy: 116 commands, 311 events, 163 telemetry channels, 34 telemetry packets
  63 type definitions (of 99 in the dictionary; the rest are unreachable from any mirrored member)
  wrote     Ref/RefProxy/CMakeLists.txt
  wrote     Ref/RefProxy/RefProxy.fpp
  wrote     Ref/RefProxy/RefProxyTypes.fpp
  wrote     Ref/RefProxy/RefProxy.hpp
  wrote     Ref/RefProxy/RefProxy.cpp
  wrote     Ref/RefProxy/RefProxyCommands.cpp
  wrote     Ref/RefProxy/RefProxyEvents.cpp
  wrote     Ref/RefProxy/RefProxyTelemetry.cpp
  wrote     Ref/RefProxy/RefProxyPackets.cpp

Add it to the build with:
  add_fprime_subdirectory("${CMAKE_CURRENT_LIST_DIR}/RefProxy")
```

## Installation

```sh
pip install git+https://github.com/fprime-community/fprime-dict-to-cpp.git
```

Requires Python 3.10 or newer. The C++ comes out through
[`fprime-cpp-codegen`](https://github.com/fprime-community/fprime-cpp-codegen); the FPP
is emitted directly. Nothing else is needed at runtime.

## What gets generated

| File | What it is |
| --- | --- |
| `<Component>.fpp` | the component: ports, and one declaration per mirrored command, event and channel |
| `<Component>Types.fpp` | the remote's enums, arrays, structs and aliases, re-rooted into the component's module |
| `<Component>.hpp` | the implementation class, deriving from the autocoded `<Component>ComponentBase` |
| `<Component>.cpp` | constructor, packet dispatch, and the shared helpers |
| `<Component>Commands.cpp` | one handler per forwarded command |
| `<Component>Events.cpp` | the event decoder |
| `<Component>Telemetry.cpp` | the channelized-telemetry decoder |
| `<Component>Packets.cpp` | one decoder per packetized-telemetry layout |
| `CMakeLists.txt` | `register_fprime_module`, listing both `.fpp` files as autocoder inputs |

The implementation is split across translation units because a dictionary the size of
`Ref`'s produces several thousand lines of decoder, and one compilation unit holding all
of it is slow to build and awkward to read.

### Ports

```
async input port comIn: Fw.Com     # packets from the remote deployment
output port comOut: Fw.Com         # command packets to the remote deployment
sync input port run: Svc.Sched     # queued components only: drains the queue
```

plus the usual command, event, text-event, telemetry and time-get special ports.
Connect `comIn` downstream of whatever deframes the remote's link, and `comOut` upstream
of whatever frames it.

## Options

### Component kind

`--kind {active,queued,passive}` decides **where an inbound packet is decoded**, and
therefore what the proxy's own commands can be:

| Kind | `comIn` | Decoding happens on | Commands |
| --- | --- | --- | --- |
| `active` (default) | `async input port` | the component's own thread | `async` |
| `queued` | `async input port` | whatever drives `run` | `async` |
| `passive` | `sync input port` | the caller's thread | `guarded` |

A queued component owns a queue but no thread, so its `run` handler calls
`dispatchCurrentMessages()`; connect it to a rate group. A passive component cannot
declare an `async` command however the remote declared it, so its commands are `guarded`.

### Name mangling

A dictionary names a command `Ref.dpDemo.SelectColor` — a topology instance path plus the
member as its component declared it. A proxy re-declares that member on *one* component,
so the dotted name has to collapse into a single identifier.

| `--naming` | `Ref.dpDemo.SelectColor` | `CdhCore.cmdDisp.CMD_NO_OP` |
| --- | --- | --- |
| `instance` (default) | `dpDemo_SelectColor` | `cmdDisp_CMD_NO_OP` |
| `qualified` | `Ref_dpDemo_SelectColor` | `CdhCore_cmdDisp_CMD_NO_OP` |
| `source-case` | `RefDpDemoSelectColor` | `CDH_CORE_CMD_DISP_CMD_NO_OP` |
| `member` | `SelectColor` | `CMD_NO_OP` |

`source-case` keeps each member's own convention: a `SCREAMING_SNAKE` member stays
screaming and a `CamelCase` member stays camel. A member with no coherent convention
(`BA_DrainBuffers`) has nothing to preserve, so it falls back to underscores.

The presets are combinations of two knobs, which `--name-qualification
{member,instance,full}` and `--name-style {underscore,source-case}` expose directly.

Whatever you pick, **uniqueness is guaranteed**. A group of names that would collide is
re-qualified — only that group, so names that were already distinct keep the short form
you asked for — and only if full qualification still collides does a numeric suffix
appear. Uniqueness is enforced case-insensitively, because `fpp-to-cpp` derives
`EVENTID_<UPPER>` and friends by upper-casing the member name.

### Other options

| Option | Effect |
| --- | --- |
| `-c`, `--component` | the component's name (default `<Deployment>Proxy`) |
| `-n`, `--namespace` | the FPP module for the component and its types (default: the component name) |
| `--no-packetized` | skip `FW_PACKET_PACKETIZED_TLM` decoding |
| `--no-diagnostics` | omit the proxy's own health telemetry and events |
| `--type-prefix` | prepend a prefix to every generated type name |
| `--include-prefix` | path prefix for the `#include` of the autocoded base class |
| `--clang-format [STYLE]` | run the generated C++ through `clang-format` |
| `--cross-check` | also parse the dictionary with `fprime-gds` and report disagreements |
| `--dry-run` | report what would be generated without writing |

## Diagnostics

Everything else the proxy reports belongs to the remote, so without something of its own
a link that is dropping or garbling packets would look identical to a link that is
simply quiet. It therefore declares:

* `ProxyPacketsDecoded`, `ProxyDecodeErrors`, `ProxyPacketsIgnored` — counters;
* `ProxyDecodeError(descriptor, identifier, status)` — warning high, throttled;
* `ProxyPacketIgnored(descriptor, identifier)` — diagnostic, throttled. Expected for
  packet kinds outside the proxy's scope, such as files and data products.

Their local ids come *after* every mirrored member's, so `--no-diagnostics` renumbers
nothing.

## Design notes

### Remote ids versus local ids

The remote's opcodes and event/channel ids are **absolute** — they already include its
components' base ids — and they are what appears on the wire. The proxy's own FPP
declares **dense local ids from zero**, which F Prime offsets by whatever base id the
target topology assigns the instance. So a forwarded command carries the *remote* opcode
in its packet while being declared with a local one, and an inbound event is matched on
the *remote* id before being emitted through a locally-numbered event.

### Remote type widths

A dictionary records the widths the remote actually serialized with — its
`FwPacketDescriptorType`, `FwOpcodeType`, `FwEventIdType`, `FwChanIdType`,
`FwTlmPacketizeIdType` — and the generated code reads every packet header field at
*those* widths rather than at the local platform's. Aliases are re-declared resolved to
their underlying built-in for the same reason. This is what lets a proxy talk to a
deployment whose `FpConfig.h` differs from its own. `Ref`, for instance, uses a 16-bit
packet descriptor where the framework default is 32-bit.

Two things cannot be handled that way, because the framework deserializes them itself
using the local configuration: `Fw::Time`, and the length prefix on a string. The
generated source carries `static_assert`s comparing the local sizes against the remote's
recorded ones, so a configuration mismatch is a build error rather than every timestamp
or every string being silently misread.

### Packetized telemetry uses fixed-width slots

`Svc::TlmChan` packs channelized telemetry as `[id][time][value]` triples end to end,
as many as fit in one buffer — so the decoder loops until the buffer is exhausted, and
an unrecognised channel id stops it, because a value's length is only knowable from its
channel's type.

`Svc::TlmPacketizer` is different: `[packet id][time][slots...]`, where every channel
occupies a slot as wide as its type's **maximum** serialized size whatever the value
took. So a packetized decoder seeks to each slot rather than reading straight through.
Reading straight through happens to work for scalars, which is what makes this an easy
bug to ship.

### Event timestamps

`tlmWrite_<Chan>` accepts an explicit `Fw::Time`, so a relayed channel keeps the
remote's timestamp. The autocoded `log_<SEVERITY>_<Event>` helpers do not — they stamp
every event from the component's own time port — so a relayed event carries the local
receive time. The remote's timestamp is still read off the wire, because the arguments
after it have to be found, and then discarded.

### Type names are flattened

`Ref.DpDemo.ColorEnum` is re-declared as `Ref_DpDemo_ColorEnum`, not as a nested module
path. `fpp-to-cpp` derives a generated file name from a type's *leaf* name plus its
enclosing component and ignores enclosing modules, so two same-leaf types in different
modules abort the autocoder with `error: duplicate C++ file`. The `Ref` dictionary
contains two such pairs (`DpReqType`, `Enabled`), so this is not hypothetical.

Only types **reachable** from a mirrored member are emitted — 63 of `Ref`'s 99. An
unused definition costs a generated C++ file pair, and one of them actively breaks the
build: a stray `FwSizeStoreType` shadows FPP's implied use of that name and takes every
string in the module down with it.

### Known fidelity limits

* **String capacities.** `string size N` does not produce an N-character C++ type at a
  component's API boundary: a command argument arrives as `Fw::CmdStringArg`
  (`FW_CMD_STRING_MAX_SIZE`, 40 by default) and event and telemetry values are clamped to
  `FW_LOG_STRING_MAX_SIZE` / `FW_TLM_STRING_MAX_SIZE`. The remote clamped the same way,
  so nothing is lost *relative to talking to it directly* — but the two deployments must
  configure the limits identically. The generator reports the counts; `Ref` has 20
  command arguments and 77 event arguments above the defaults.
* **Command responses.** An `OK` response from a forwarded command means the packet was
  sent, not that the remote executed it. The remote's dispatcher reports that as one of
  its own events, which the proxy relays separately.
* **Command packet size.** If a command's arguments do not fit in `Fw::ComBuffer`, the
  handler responds `FORMAT_ERROR` rather than sending a truncated packet.
* **Out of scope.** Parameters are not mirrored as parameters — FPP's generated
  `PRM_SET`/`PRM_SAVE` pair appears in the dictionary's `commands` section and is
  forwarded like any other command. Data products (records and containers) are not
  mirrored at all.

## Development

```sh
python -m venv venv
venv/bin/pip install -e '.[dev]'
venv/bin/python -m pytest
venv/bin/python -m mypy          # strict
venv/bin/python -m black src tests
```

### Seeing what it generates

`tests/goldens/RefProxy/` holds the **complete generated component** for the Ref
dictionary — all nine files, ~17,000 lines — checked in and compared byte for byte. It is
what `fprime-dict-to-cpp tests/data/RefTopologyDictionary.json -o RefProxy` produces with
every default, so reading it is the fastest way to see what this tool actually emits. It
is also the review surface: a change anywhere in the generator turns up as a diff of real
F Prime code rather than as a passing test suite and a shrug.

```sh
venv/bin/python -m pytest tests/test_goldens.py    # compare
FDTC_UPDATE_GOLDENS=1 venv/bin/python -m pytest tests/test_goldens.py
git diff tests/goldens                             # then review the diff
```

An integration test copies that same checked-in tree out, autocodes it and compiles it,
so the files in the repository cannot drift into being unbuildable while the suite still
passes.

### Testing against a real toolchain

Unit tests check that the generator produces the text it meant to. The tests marked
`integration` check the only thing that finally matters: that `fpp-check` accepts the
model, `fpp-to-cpp` autocodes it, and a C++ compiler builds the implementation against
the base class that comes out — warning-free under `-Wall -Wextra`, for all three
component kinds and all four naming presets.

They look for an F Prime checkout at `~/git/fprime` or wherever `FPRIME_ROOT` points, and
skip themselves when there is not one. Compiling additionally needs a checkout that has
been built at least once, since the autocoded base class includes framework headers that
do not exist until then.

`tests/build_probe.py` runs the same path by hand and keeps the output:

```sh
venv/bin/python tests/build_probe.py /tmp/proxy
```

The example dictionary in `tests/data/` is `Ref`'s, from F Prime's own test deployment:
116 commands, 311 events, 163 channels, 99 type definitions and 34 telemetry packets.
It is a good exercise of the awkward cases — 23 events with an argument called `status`,
two type leaf-names that collide across modules, 59 formal parameters that are FPP
keywords, and an event and a telemetry channel that share a name.
