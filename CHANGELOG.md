# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org);
each release is tagged `vX.Y.Z` and published on the
[releases page](https://github.com/saalse/context7-add/releases).

## [1.2.0] - 2026-11-22

### Added
- `add` takes several URLs and validates all of them before sending any. It prints one
  progress line per URL, and Ctrl-C or SIGTERM prints the partial summary (exit 130).
- `add --queue` waits for a free slot when Context7's queue of 5 submissions per user is
  full (`--queue-timeout` per URL).
- `upload` checks the file size before sending, with a split hint per file type, and
  `--skip-size-check` to override it.
- `--help` text for every command and flag.
- Docs: the **Upload a file** page, the **Add many sources** section, new gotchas and exit
  codes.
- Offline tests (Context7 API stubbed), CI on Python 3.8 to 3.14 with skill validation and
  coverage, a release workflow, and README badges.

### Changed
- A full queue now exits 9, not 2; a 429 or a 401 stops the run. The inputs still to send
  are listed in `notSubmitted`; other refusals and network errors in `failed`.
- Uploads over 4.4 MB are refused before sending (Context7 answers 413 above about 4.5 MB).
- Examples use this repo where a real source fits.

### Fixed
- Network failures come back as JSON instead of a traceback.
- A git token is sent with repositories only, never with websites.
- `log` keeps stdout JSON when nothing has been logged.

## [1.1.0] - 2026-11-17

First public version (not tagged): `whoami`, `check`, `allow`, `add`, `upload`, `refresh`,
`wait` and `log`.

[1.2.0]: https://github.com/saalse/context7-add/releases/tag/v1.2.0
[1.1.0]: https://github.com/saalse/context7-add/tree/a6f1fb7
