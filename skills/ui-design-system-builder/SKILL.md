---
name: ui-design-system-builder
description: Apply Apple-quality interaction and accessibility principles to automatically built web and app interfaces, select an approved UI library, and prevent unnecessary custom component work.
---

# UI Design System Builder

Use this skill before and during any generated app, web interface, dashboard, or client-facing UI implementation.

## Required inputs

- target platform(s) and framework;
- product purpose, primary user, and highest-risk interaction;
- existing package manifest and design-system dependencies;
- brand constraints, localization needs, accessibility requirements, and performance budget.

## Workflow

1. Read `docs/14-design-system-and-ui-library-policy.md` and `data/ui-library-registry.yaml`.
2. Inspect the existing repository before adding dependencies. Preserve an existing coherent system unless there is an explicit migration decision.
3. Choose one primary component system for each surface. Use platform-native controls when the platform is the product's interaction language.
4. Produce a design brief and component map before implementing repeated UI.
5. Define semantic tokens for color roles, typography, spacing, shape, elevation/material, motion, focus, and z-order. Keep tokens separate from feature logic.
6. Compose approved primitives. Build custom code only for product-specific composition or a documented library gap.
7. Apply Apple-quality interaction rules: immediate press feedback, direct manipulation, interruptible transitions, velocity handoff, spatially anchored popovers/sheets, restrained materials, and no motion that blocks input.
8. Add reduced-motion, reduced-transparency, high-contrast, keyboard, screen-reader, Dynamic Type/large-text, localization, dark-mode, loading, error, empty, and overflow states.
9. Validate with component tests, keyboard/focus checks, responsive screenshots, and a performance pass. For touch gestures, test on a real device when available.

## Companion skills

Use `apple-design` for Apple interaction and motion decisions, `emil-design-eng` for interface craft, `animate` or `animate-expo` for motion-heavy work, `react-native-best-practices` and `vercel-react-native-skills` for React Native/Expo performance, `react-native-testing` for component tests, and `web-perf` for browser performance evidence when those skills match the stack.

## Hard rules

- Do not invent a Button, Dialog, Menu, Select, Combobox, Tooltip, Toast, Tabs, Date Picker, Table, or navigation primitive when the selected library already provides one.
- Do not mix MUI, Base UI, Radix, React Aria, or another full system casually on one surface.
- Do not make every platform look like iOS. Apply Apple principles of clarity, agency, craft, responsiveness, and restraint while honoring Android, web, and Flutter conventions.
- Do not use `transition: all`, animation as an input delay, non-interruptible gesture motion, or decorative bounce without physical cause.
- Do not hide focus, rely on color alone, use icon-only controls without accessible names, or ship a modal without focus management and Escape/close behavior.
- Do not add a dependency without checking tree-shaking, bundle impact, license, maintenance, platform support, and accessibility behavior.

## Completion contract

Return or commit:

- the design brief;
- selected registry entries and the reason for each;
- token definitions;
- component map;
- accessibility and reduced-motion checks;
- responsive/state screenshots or equivalent evidence;
- a list of any custom primitives and the library gap that justified each one.
