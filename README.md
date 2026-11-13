# TokenEconomizer for ZCode

[TokenEconomizer](https://github.com/microsoft/TokenEconomizer) (Microsoft/Berkeley,
arXiv 2610.22732) as a native ZCode plugin: it learns a repository's recurring
environment facts across sessions, injects them at session start, prices every session
from ZCode's own usage table, and redirects turns stuck in a failing loop. Upstream is
Apache-2.0; this channel publishes the ZCode port built from
[iztou/TokenEconomizer](https://github.com/iztou/TokenEconomizer) (branch `zcode`).

## Install

1. ZCode → Plugin Marketplace → **Add** → **Add Plugin Marketplace** → paste
   `iztou/tokeneconomizer-zcode`
2. **Personal → tokeneconomizer-zcode → TokenEconomizer → Install**

Or from a shell with the CLI:

```sh
zcode plugins marketplace add iztou/tokeneconomizer-zcode
zcode plugins install tokeneconomizer@tokeneconomizer-zcode
```

The bundle is self-contained (pure-stdlib Python); no pip, no node.

## After installing

Mechanisms start conservative (micro-skills `off`) and learn only from what they see:

```sh
~/.tokeneconomizer/bin/te status                     # per-repo state, right inside ZCode
te config set microskills=envfacts                   # LLM-free facts; no model calls
te config set microskills=envfacts+distiller         # + a paid proposer through zcode -p
te config set zcode_sidecall=on --globally           # allow those zcode -p calls (~275k
                                                     # input tokens each, your plan)
```

Tool pruning is not available on ZCode (its MCP tools lazy-load, so there is nothing to
prune). Everything the plugin stores is under `~/.tokeneconomizer`; ZCode's own session
database is only ever read.

## Updates

The marketplace refresh picks up a new version whenever this repository moves. The
source branch and every change to it: [iztou/TokenEconomizer `zcode`](https://github.com/iztou/TokenEconomizer/tree/zcode).
