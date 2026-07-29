# Examples

Sample documents you can open in LaTeX Studio.

## `showcase.tex`

A one-page overview of the project — and a deliberate exercise of the features
the compiler has to get right: TikZ diagrams, `tcolorbox` panels, tables,
custom colours, icon fonts, hyperlinks and display mathematics.

**Try it:** launch the app, drag `showcase.tex` onto the upload zone, press
**Compile**. It renders to a single page in a few seconds.

It is also a useful smoke test after changing the compiler: a healthy build
reports **no errors, no warnings and no overfull boxes**. Anything else is a
regression worth looking at.

Requirements: pdfLaTeX only — no shell-escape, and every package it uses ships
with the bundled TinyTeX.

> The compiled `showcase.pdf` is intentionally not committed. Like every other
> build artifact in this repository it is regenerated rather than stored — see
> `.gitignore`.
