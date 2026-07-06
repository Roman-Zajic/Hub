[TOC]
---
# 1. Headers & Separators
Use hash symbols `#` for headers. The more hashes you use, the smaller the header becomes. Use three dashes `---` on a new line to create a horizontal separator.

**How to write it:**
```
# Heading 1
## Heading 2
### Heading 3
```
---
# 2. Blockquotes & Typography
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
# 3. Admonitions (Callouts)
Admonitions are great for highlighting important information. Use `!!! type "Title"` followed by indented content. Supported types are `info` (default), `warning`, and `danger` (or `error`).

**How to write it:**
```
!!! info "Information (Teal)"
    This is the default style for general notes and info.
!!! warning "Warning (Amber)"
    This is for items that require attention. It has a light amber background.
!!! danger "Error (Red)"
    This is for critical errors or dangerous actions. It has a light red background.
```

**Result:**
!!! info "Information (Teal)"
    This is the default style for general notes and info.
!!! warning "Warning (Amber)"
    This is for items that require attention. It has a light amber background.
!!! danger "Error (Red)"
    This is for critical errors or dangerous actions. It has a light red background.
---
# 4. Lists & Checklists
Standard unordered (`-` or `*`) and ordered (`1.`) lists are supported.
For task checklists, use `- [ ]` for incomplete and `- [x]` for completed tasks. The custom styling automatically removes the standard bullet points and perfectly aligns the checkboxes.

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
# 5. Tables
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
# 6. Code Blocks
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
# 7. Custom Line Breaks
Our editor features custom line-break logic for plain text. 
- Pressing **Enter once** creates a standard paragraph break.
- Pressing **Enter multiple times** adds visible vertical spacing gaps corresponding to the number of empty lines you leave.

**How to write it:**
```
This line is separated by one Enter (New Paragraph).

This line is separated by two Enters.
It has a visible gap above it.
```