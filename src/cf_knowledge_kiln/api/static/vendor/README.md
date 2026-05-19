# Vendored third-party assets

These files are committed verbatim from their upstream distributions so the
runtime CSP can be `script-src 'self'; font-src 'self'; style-src 'self'`
without an allowlist for any third-party origin. See `api/csp.py` and #144.

DO NOT modify these files in place. Rotate by replacing the file with a
fresh upstream download and updating the version suffix in the filename +
the integrity hash in `api/templates/base.html` if applicable.

| File | Source | Version | License | SHA-384 (base64) |
| --- | --- | --- | --- | --- |
| `htmx-2.0.4.min.js` | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` | 2.0.4 | [Zero-Clause BSD (0BSD)](https://opensource.org/license/0BSD) | `HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+` |
| `fraunces-v38-latin-variable.woff2` | `https://fonts.gstatic.com/s/fraunces/v38/6NU78FyLNQOQZAnv9bYEvDiIdE9Ea92uemAk_WBq8U_9v0c2Wa0KxC9TeA.woff2` | v38 (latin subset, variable axes opsz/wght/SOFT) | [SIL Open Font License 1.1](https://openfontlicense.org/) | n/a |
| `jetbrains-mono-v24-latin-400.woff2` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwg.woff2` | v24 (latin subset, wght 400) | [SIL Open Font License 1.1](https://openfontlicense.org/) | n/a |

Latin unicode-ranges used by the `@font-face` rules in `static/kiln/_fonts.css`
match the subsets shipped at those upstream URLs (`U+0000-00FF` + the standard
latin punctuation/symbols block).

To regenerate the htmx SHA-384 for the `integrity=` attribute:

```bash
openssl dgst -sha384 -binary src/cf_knowledge_kiln/api/static/vendor/htmx-2.0.4.min.js \
  | openssl base64 -A
```
