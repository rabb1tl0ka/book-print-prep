# book-print-prep

Turns a scanned two-pages-per-spread PDF into a print-ready PDF for loose-leaf (double fan) binding.

Needs `pdftoppm`, `pdfinfo`, `pdfunite` (poppler), Python 3 with Pillow and numpy.

## What it does
- Drops red ink (red channel only), flattens yellowed paper to white, boosts contrast to fade light pencil
- Splits each spread into left and right pages, aligns each page's text block, crops away margin notes and scan edges
- Adds a spine margin (mirrored for left and right pages) and lays two leaves per landscape sheet in cut-and-stack order
- After printing duplex and cutting each sheet in half: stack all left halves, then all right halves

## Usage
```
python3 bookprep.py book.pdf --first 2 --work /tmp/work --out out/book-A4.pdf
python3 bookprep.py book.pdf --first 2 --work /tmp/work --out out/test.pdf --test-sheet 40   # print this first
```
Useful flags: `--paper a4|letter`, `--levels 0.55 0.92` (raise the first to fade pencil harder), `--margin`, `--gutter`,
`--flip short|long` (match your printer's duplex setting), `--blank 286R,287L` (blank handwriting-only pages),
`--no-blank-front`. Stage 1 is cached in `--work`; use `--force` after changing `--levels`.

## Gotchas
- Print a `--test-sheet` first: duplex flip direction differs per printer.
- Handwriting inside the text block is not removed, only handwriting in the margins. Blank those pages with `--blank`.
- Skip the cover spread with `--first 2`.

## License

[0BSD](LICENSE): do whatever you want with it.
