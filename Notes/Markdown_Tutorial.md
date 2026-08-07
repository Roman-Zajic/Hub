[TOC]

---
# 1. Headers & Separators
Use hash symbols `#` for headers. There are three levels available — the more hashes you use, the smaller the header becomes:

- `#` — large title, meant for the top of a note
- `##` — section heading, underlined
- `###` — small heading

Use three dashes `---` on their own line to create a horizontal separator between sections.

**How to write it:**
```
# Heading 1
## Heading 2
### Heading 3

---
```

---
# 2. Text Formatting
Standard inline formatting is supported:

- `**bold text**` for **bold text**
- `*italic text*` for *italic text*
- `` `inline code` `` for `inline code`
- `[[Note Name]]` for links to other notes (see section 10)
- `\(x\)` for inline math (see section 9)

You can combine bold and italic, and inline code is always protected from every other rule on this page — line-break logic, wiki-link conversion, math, and so on all leave the contents of a `` `backtick span` `` completely alone.

---
# 3. Blockquotes
Use the greater-than sign `>` to create blockquotes. Our custom styling gives them a thick amber left border and italicized text. You can also nest them by adding additional `>` signs.

**How to write it:**
```
> This is a standard blockquote. It is styled with an amber border.
>
> > This is a nested blockquote. It maintains professional indentation.
```

**Result:**
> This is a standard blockquote. It is styled with an amber border.
>
> > This is a nested blockquote. It maintains professional indentation.

---
# 4. Admonitions (Callouts)
Admonitions are great for highlighting important information. Use `!!! type "Title"` followed by one or more indented lines of content. Supported types are `info` (default), `warning`, and `danger` (or `error`) — each one is colored so its text stays readable against its own background, rather than everything defaulting to the same brand teal.

**Callouts can span multiple paragraphs.** Any indented line stays part of the callout, and a blank line inside the block is fine too, as long as more indented text follows it — the whole thing is still treated as one callout. A blank line that ISN'T followed by more indented text ends the callout, exactly the way you'd expect.

**Line breaks inside a callout now behave like normal text too:** press Enter once to start a new line within the same paragraph (shown tightly, without extra spacing), or leave a blank line to start a whole new paragraph inside the callout (shown with the wider paragraph gap). You no longer need a blank line just to make a line break visible.

**How to write it:**
```
!!! info "Information (Teal)"
    This is the default style for general notes and info.
    This second line sits right under the first one — no
    blank line was needed to break it onto its own line.

    This is a separate paragraph inside the same callout,
    started with a blank line above it.

!!! warning "Warning (Amber)"
    This is for items that require attention. It has a light
    amber background, and the text color is tuned to match.

!!! danger "Error (Red)"
    This is for critical errors or dangerous actions. It has
    a light red background, again with matching text color.
```

**Result:**
!!! info "Information (Teal)"
    This is the default style for general notes and info.
    This second line sits right under the first one — no
    blank line was needed to break it onto its own line.

    This is a separate paragraph inside the same callout,
    started with a blank line above it.

!!! warning "Warning (Amber)"
    This is for items that require attention. It has a light
    amber background, and the text color is tuned to match.

!!! danger "Error (Red)"
    This is for critical errors or dangerous actions. It has
    a light red background, again with matching text color.

---
# 5. Lists & Checklists
Standard unordered (`-` or `*`) and ordered (`1.`) lists are supported.
For task checklists, use `- [ ]` for incomplete and `- [x]` for completed tasks. The custom styling automatically removes the standard bullet points and perfectly aligns the checkboxes — and checkboxes are clickable directly in the preview, toggling the underlying markdown for you.

**How to write it:**
```
- Standard list item 1
- Standard list item 2

1. Ordered item 1
2. Ordered item 2

- [x] This task is finished
- [ ] This task is still open
- [ ] Checkboxes are perfectly aligned with this text
```

**Result:**
- Standard list item 1
- Standard list item 2

1. Ordered item 1
2. Ordered item 2

- [x] This task is finished
- [ ] This task is still open
- [ ] Checkboxes are perfectly aligned with this text

---
# 6. Tables
Create tables using pipes `|` and hyphens `-`. Our tables feature custom borders, alternating hover effects, and clean padding. The alignment is controlled by the colons `:` in the second row.

**How to write it:**
```
| Feature | Status | Color |
| :--- | :--- | :--- |
| Blockquotes | Implemented | Amber Border |
| Checklists | Fixed | No Bullets |
| Code Blocks | Fixed | Fenced |
```

**Result:**
| Feature | Status | Color |
| :--- | :--- | :--- |
| Blockquotes | Implemented | Amber Border |
| Checklists | Fixed | No Bullets |
| Code Blocks | Fixed | Fenced |

---
# 7. Code Blocks
Wrap your code in triple backticks. The custom preview automatically protects the code from line-break formatting and adds a handy "Copy" button to the top right of the block.

**How to write it:**
Use triple ` symbol to begin and end the block of code.

**Result:**
```
def check_logic():
    # This code block is clean
    # and the 'Copy' button is in the top right
    status = "Working"
    return f"Logic is {status}"
```

---
# 8. Custom Line Breaks
Our editor features custom line-break logic for plain text.
- Pressing **Enter once** creates a standard paragraph break.
- Pressing **Enter multiple times** adds visible vertical spacing gaps corresponding to the number of empty lines you leave.

This custom spacing applies to plain paragraph text. Tables, checklists, and code blocks each have their own line-handling rules described above; admonitions now follow the SAME rules as plain text (Section 4), just contained inside the callout box.

**How to write it:**
```
This line is separated by one Enter (New Paragraph).

This line is separated by two Enters.
It has a visible gap above it.
```

---
# 9. LaTeX / Math
Mathematical notation renders live in the preview, powered by MathJax. Two kinds of delimiters are supported:

| Type | Delimiters | Example source |
| :--- | :--- | :--- |
| Inline | `\( ... \)` | `\(E = mc^2\)` |
| Display (its own centered block) | `$$ ... $$` or `\[ ... \]` | `$$ x = \frac{-b \pm \sqrt{b^2-4ac}}{2a} $$` |

**A plain single `$...$` is deliberately NOT used for math** — that clashes with ordinary dollar amounts like "$100 and $200" showing up anywhere else in a note, which would otherwise get silently swallowed as a (broken) equation.

Math is protected from every other rule on this page, the same way a code block is — line-break logic, wiki-link conversion, and bold/italic parsing all leave the contents of your formula completely alone, so `_`, `*`, and `\` inside a formula always mean exactly what LaTeX expects.

**How to write it:**
```
Inline example: the identity \(a^2 + b^2 = c^2\) is the Pythagorean theorem.

Display example:

$$
x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}
$$

Or using the \[ \] form:

\[
\sum_{i=1}^{n} i = \frac{n(n+1)}{2}
\]
```

**Result:**

Inline example: the identity \(a^2 + b^2 = c^2\) is the Pythagorean theorem.

Display example:

$$
x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}
$$

Or using the \[ \] form:

\[
\sum_{i=1}^{n} i = \frac{n(n+1)}{2}
\]

---
# 10. Linking Between Notes
You can create clickable links that jump straight to another note. Wrap the note's name in double square brackets `[[ ]]`. Links render in teal, matching the brand palette (not the default blue).

If the note lives in a folder, include the folder in the link, e.g. `[[Work/Meeting Notes]]`. You can also show a different display text by adding a pipe `|` after the note name.

**How to write it:**
```
[[Markdown Tutorial]]

[[Work/Meeting Notes]]

[[Markdown Tutorial|Click here for the tutorial]]
```

**Result:**
[[Markdown Tutorial]]

[[Markdown Tutorial|Click here for the tutorial]]

---
# 11. External Links
Regular markdown links — `[label](https://example.com)` — work for linking out to the web, separately from the `[[note]]` syntax above. Any link pointing to an `http://` or `https://` address opens in a new browser tab automatically, so you're never navigated away from your notes. Links to other notes and links within the same page (like the table of contents below) still open in place, exactly as before.

**How to write it:**
```
[Anthropic](https://www.anthropic.com)
```

**Result:**
[Anthropic](https://www.anthropic.com)

---
# 12. Linking to Files on Your Computer
A regular markdown link can also point at a file on this computer instead of a website — a report, a spreadsheet, a PDF, anything sitting on disk. Any of these path styles are recognized automatically:

- `C:\Users\Name\Documents\report.pdf` (backslashes)
- `C:/Users/Name/Documents/report.pdf` (forward slashes)
- `\\server\share\file.docx` (a network/UNC path)
- `file:///C:/Users/Name/Documents/report.pdf`

A browser can't launch your computer's default app for a file directly — that's blocked for security reasons no matter how the link is written. So instead, clicking one of these links downloads the file to your normal Downloads folder; open it from there with whichever app you'd normally use. These links render in amber (rather than the teal used for note links) so they're visually distinct at a glance.

**How to write it:**
```
[Quarterly Report](C:\Users\Roman\Documents\Quarterly Report.pdf)
```

**Result:**
[Quarterly Report](C:\Users\Roman\Documents\Quarterly Report.pdf)

*(This particular example points at a path that doesn't exist on your machine, so clicking it will just show "File not found" — replace it with a real path on your computer to try it for real.)*

---
# 13. Table of Contents
Add `[TOC]` on its own line anywhere in a note (typically at the very top) to generate a clickable table of contents from your `#`/`##`/`###` headers. It's boxed with a square-cornered border and a teal accent on the left, and every entry jumps straight to that section within the same page.

**How to write it:**
```
[TOC]
```

The box at the very top of this tutorial is a live example.
