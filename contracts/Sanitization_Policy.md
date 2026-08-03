# Governed sanitization policy

`rey_lib.files.sanitize_file()` supplies streaming mechanics. YAML supplies every
active character or line-repair rule. Present rules are active; absent or commented
rules are inactive. There are no hidden removal or replacement defaults.

Each inline File Operator process declares `sanitization.global`, named
`sanitization.feeds`, and a `feed` on every manifest selection. The global layer
is inherited. A feed rule for the same Unicode code point replaces the global
classification; otherwise it is added. Effective tables are validated, frozen for
the run, and hashed before source processing.

## Character tables and precedence

Keys use uppercase `U+` syntax with four to six hexadecimal digits, such as
`U+0009` or `U+00A0`. Surrogates and noncanonical spellings fail validation.
Every entry needs `name` and `reason`; `replace` also needs `with`.

Precedence is: quote state; `preserve_if_quoted` while quoted; `preserve`;
`remove`; `replace`; true-newline normalization; configured `line_repair`; then
preservation of every unclassified character. A code point cannot occur in two
tables within one layer. To override a global removal for one feed, place that
code point in the feed's `preserve` table.

The pinned quote character is `"`. A non-doubled quote toggles lexical quote
state; doubled quotes inside quoted text remain quoted. CR, LF, and CRLF inside
quotes are preserved only by the corresponding `preserve_if_quoted` rules. True
CR, LF, and CRLF outside quotes become LF.

NBSP, smart quotes, en/em dashes, tabs, and FILE/GROUP/RECORD/UNIT separators may
be valid data. They change only through explicit effective rules.

## Encoding resolution and destination

The workflow supplies one complete `outbox` path, including the destination
filename. File Operator resolves that template and the shared sanitizer publishes
exactly to that path. The sanitizer does not construct filenames or extensions.

Source encoding is resolved deterministically, not guessed. A recognized UTF-8,
UTF-16 LE/BE, or UTF-32 LE/BE BOM wins. Without a BOM, the complete file is
strictly validated first as UTF-8 and then as Windows-1252. Failure of both
validations fails closed. ASCII resolves as UTF-8. Latin-1 and probabilistic
detectors are not used. Unsupported BOM signatures fail closed, and decoding
never uses `errors="ignore"` or `errors="replace"`.

Evidence records the resolved encoding, `bom`, `utf8_validation`, or
`windows_1252_fallback` resolution method, BOM type and presence, and whether
publication changed the encoding. Validation and processing are streaming and
do not require whole-file memory.

## Complete global and feed example

```yaml
sanitization:
  global:
    policy_name: platform_data_feed
    policy_version: "1.0"
    remove:
      U+0000: {name: "NULL", reason: invalid embedded null}
      U+0008: {name: BACKSPACE, reason: non-printing editing control}
    preserve:
      U+0009: {name: TAB, reason: legitimate feed delimiter}
      U+001C: {name: FILE_SEPARATOR, reason: legitimate legacy structure}
    preserve_if_quoted:
      U+000A: {name: LF, reason: embedded quoted newline}
      U+000D: {name: CR, reason: embedded quoted newline}
    replace: {}
    line_repair: {}
  feeds:
    bmo:
      policy_name: bmo
      policy_version: "1.0"
      remove: {}
      preserve: {}
      preserve_if_quoted: {}
      replace:
        U+00A0:
          name: NON_BREAKING_SPACE
          with: " "
          reason: BMO downstream contract requires an ordinary space
      line_repair: {}
```

Commenting out `U+0008` under `global.remove` disables that global removal.
Moving it to `feeds.bmo.preserve` retains it for BMO while other feeds inherit
the global removal.

## Adding a new feed

Copy an existing feed overlay, give it an immutable name/version, and list only
its differences from global. No Python edit is needed:

```yaml
sanitization:
  feeds:
    northern_trust:
      policy_name: northern_trust
      policy_version: "1.0"
      remove:
        U+001A: {name: SUBSTITUTE, reason: prohibited by source contract}
      preserve: {}
      preserve_if_quoted: {}
      replace: {}
      max_logical_line_characters: 1048576
      line_repair:
        trailing_space:
          pattern: '[ \t]+$'
          replacement: ''
          reason: feed contract prohibits trailing whitespace
```

Then set `feed: northern_trust` on that feed's manifest selection. Line repairs
run in YAML order at completed true-line boundaries. They require a positive
`max_logical_line_characters`; supported flags are `ASCII` and `IGNORECASE`.
Patterns that can match an empty string fail validation.

Evidence records global/feed names and versions, the deterministic effective
policy SHA-256 digest, source/destination hashes and sizes, counts for each
remove, preserve, preserve-if-quoted, replacement and line-repair rule, true
newline normalization count, destination, file ID, and mutation references. It
never records source text, values, or positions.
