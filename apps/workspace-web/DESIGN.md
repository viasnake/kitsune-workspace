# Design System

## Theme

Kitsune Workspace uses a restrained light product theme for dense operational work in a bright NOC. Pure white is the dominant surface. A low-chroma green-tinted neutral separates navigation, table headers, form groups, and secondary regions without reducing contrast. The olive-green anchor is reserved for navigation selection, primary actions, links, focus relationships, and live state; it is not decoration.

## Color

All implementation colors use OKLCH tokens in `src/styles.css`.

| Role | Token | Value | Use |
| --- | --- | --- | --- |
| Background | `--bg` | `oklch(1 0 0)` | Page and primary component surfaces |
| Subtle surface | `--surface-subtle` | `oklch(0.975 0.006 160)` | Sidebar, toolbars, table headers, structured groups |
| Muted surface | `--surface-muted` | `oklch(0.945 0.008 160)` | Hover, inactive badges, secondary controls |
| Primary ink | `--ink` | `oklch(0.19 0.025 160)` | Headings and primary body text |
| Secondary ink | `--ink-secondary` | `oklch(0.39 0.022 160)` | Labels and table values |
| Muted ink | `--ink-muted` | `oklch(0.5 0.018 160)` | Metadata and supporting text |
| Border | `--line` | `oklch(0.875 0.012 160)` | Section and row separation |
| Brand anchor | `--primary` | `oklch(0.55 0.119 160)` | Live markers and selected relationships |
| Primary action | `--primary-strong` | `oklch(0.38 0.095 160)` | Filled actions, active links, selected navigation |
| Focus | `--focus` | `oklch(0.62 0.16 245)` | Keyboard focus rings |
| Danger | `--danger` | `oklch(0.49 0.18 27)` | Failed states and destructive actions |
| Warning | `--warning` | `oklch(0.55 0.13 75)` | Degraded and transitional states |
| Information | `--info` | `oklch(0.48 0.13 245)` | Running and dispatching states |
| Success | `--success` | `oklch(0.42 0.115 155)` | Healthy, ready, and succeeded states |

Semantic states always combine a written label, a dot, and a background/text pair. Color is never the only state indicator. Saturated fills use `--on-primary` text.

## Typography

Use one system sans family: `Inter`, `ui-sans-serif`, `system-ui`, platform UI fonts. UI text uses a compact fixed scale from `0.66rem` metadata through `1.7rem` page titles. IDs, JSON, cron expressions, log lines, and trace identifiers use the system monospace stack. Headings use moderate weight and tight but readable spacing; display typography is not used.

## Layout

- A fixed `15.5rem` sidebar and sticky `3.75rem` top bar frame desktop work.
- The main content width is capped at `100rem` and uses responsive page padding.
- Tables remain tables and scroll horizontally when necessary; operational columns are not converted into unrelated cards.
- The sidebar becomes an off-canvas navigation below `64rem`.
- Two-column detail regions collapse structurally at `64rem` or `48rem` depending on information density.
- Related facts use shared bars and definition lists. Bordered sections are used only when they establish a real data or action boundary; sections are not nested as decorative cards.

## Components

### Buttons

Buttons share one radius, weight, focus ring, disabled opacity, and loading spinner. Variants are primary, secondary, quiet, and danger-secondary. Lifecycle and cancellation buttons are absent for insufficient roles; a permission explanation replaces them. Destructive stop operations require confirmation.

### Status badges

Badges expose success, danger, warning, information, and muted tones. Every badge includes a state word and dot. Active live states may pulse; the animation stops with reduced motion.

### Tables

Tables use subtle headers, one-pixel row separators, tabular numeric values, monospace identifiers, and a consistent final-column detail action. Empty, loading, and error states replace the table body at the owning section level.

### Forms

Controls use native input, select, checkbox, and textarea affordances with visible labels and help text. Handler JSON Schema produces typed fields when its object shape is supported. Unsupported or absent schemas use a labeled JSON editor with parse errors connected through `aria-describedby`.

### Tabs

Agent detail tabs separate Definition, Descriptor, Handler, Plugin, Runtime Instance, Trigger, Schedule, Run, and Usage concerns. The selected tab uses text and an underline, not color alone. Tabs use native buttons and tab semantics.

### Operational data

JSON snapshots, event payloads, log tails, and trace IDs use monospace text and bounded scrolling. Raw logs are only tailed through the Runtime Adapter; external Runtime logs and traces remain links. Run trees preserve parent/child lineage and mark the current Run explicitly.

## Motion

Transitions last 150–200 ms and communicate navigation, hover, loading, or connection state only. There are no page entrance sequences or decorative motion. `prefers-reduced-motion: reduce` collapses animations and transitions to an immediate state change.

## Accessibility

The interface targets WCAG 2.2 AA. It has a skip link, landmark navigation, visible keyboard focus, semantic tables and forms, written state labels, live regions for counts and errors, and reduced-motion handling. Body ink is selected for at least 7:1 contrast on white; secondary text and controls meet the applicable 4.5:1 requirement.
