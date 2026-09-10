# 14 — Design System and UI Library Policy

## Decision

Every automatically built app must use a platform-appropriate design system and an existing component library before creating custom UI primitives. Apple Human Interface Guidelines are the default quality bar for interaction quality, accessibility, hierarchy, restraint, and motion on Apple platforms. They are a design foundation, not a mandate to make Android or web products look like iOS.

Primary Apple references:

- [Apple Developer Documentation](https://developer.apple.com/documentation)
- [Apple Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines)
- [SwiftUI](https://developer.apple.com/documentation/swiftui)
- [UIKit](https://developer.apple.com/documentation/uikit)

The maintained shortlist and package names live in `data/ui-library-registry.yaml`. Builders must select from that registry, record the choice in the app's design brief, and avoid mixing competing component systems within one surface.

## Apple-derived quality bar

Generated interfaces must be:

- purposeful and understandable, with the primary action obvious and advanced options progressively disclosed;
- familiar and platform-consistent, while preserving user agency, undo, previews, and safe handling of destructive actions;
- responsive on press/down and continuous during direct manipulation;
- interruptible: gesture-driven motion starts from the current presentation value, carries release velocity, and can be redirected immediately;
- spatially coherent: sheets, menus, popovers, and transitions originate from their trigger and return along a consistent path;
- restrained with motion, translucency, depth, sound, and haptics; each effect must communicate state or causality;
- legible in light/dark mode and at larger text sizes, using system/platform typography by default;
- accessible through keyboard, pointer, touch, screen reader, focus-visible states, sufficient contrast, labels, and semantic structure;
- respectful of `prefers-reduced-motion`, `prefers-reduced-transparency`, `prefers-contrast`, Dynamic Type, localization, and rotation/resizing.

For web motion, use compositor-friendly properties and an interruptible spring/inertia library such as Motion. Start with critically damped, low-drama motion; reserve bounce for momentum-driven interactions. Do not use animation to delay input or block interaction.

## Default stack choices

| Stack | Default foundation | Use when | Avoid |
|---|---|---|---|
| SwiftUI / iOS / macOS | Apple system controls + SwiftUI | Native Apple apps and Apple-specific interaction patterns | Replacing native controls for visual novelty |
| React / Next.js web | Base UI or React Aria Components + Tailwind CSS; shadcn/ui for source-owned styled starters; Motion for motion | Accessible, custom-branded web products | Building dialogs, menus, comboboxes, focus management, or toasts from scratch |
| React / Next.js enterprise or Material product | MUI | A coherent Material Design product needs a broad ready-to-use catalog, theming, and data-heavy controls | Mixing MUI and another full component system on the same screen |
| React Native / Expo | Tamagui when web/native sharing matters; React Native Paper for Material-oriented apps | Cross-platform apps with a shared token/component layer | Assuming web components or CSS can be copied directly to native |
| Flutter | Material 3 by default; Cupertino widgets for Apple-specific surfaces | Dart/Flutter apps that need adaptive Material and iOS UI | Reimplementing standard navigation, dialogs, fields, or buttons |
| Android Jetpack Compose | Compose Material 3 | Android-native apps and adaptive large-screen layouts | Applying iOS conventions where Android conventions are expected |

These are defaults, not permanent lock-in. A builder may choose another library only when the product brief records a concrete reason such as platform coverage, accessibility, performance, licensing, required component coverage, or an existing codebase constraint.

## Component reuse rules

1. Search the selected library and registry before writing a primitive.
2. Prefer the library's accessible behavior and wrap it with project tokens and a small project API.
3. Do not create a custom Button, Input, Dialog, Sheet, Menu, Select, Combobox, Tooltip, Toast, Tabs, Date Picker, Table, or navigation primitive when an approved library provides it.
4. Custom components are for product-specific composition or a documented library gap, not for re-skinning a primitive from scratch.
5. Keep one source of truth for tokens: color roles, type scale, spacing, radii, elevation/material, motion, focus, and z-order.
6. Import only the components used; prefer tree-shakable or source-owned components where that reduces bundle or dependency cost.
7. Test every interactive component in idle, hover/focus, pressed, disabled, loading, error, empty, overflow, dark, high-contrast, reduced-motion, and large-text states.

## Automatic builder output

Before implementation, the builder must produce a short design brief containing:

- target platforms and platform conventions;
- selected design system and UI library, with the reason and registry entry;
- token plan and typography choices;
- component map showing which library primitive backs each repeated pattern;
- motion and reduced-motion policy;
- accessibility and localization risks;
- known library gaps that justify custom code.

The implementation is not accepted until the brief, component map, responsive states, and accessibility checks are present. The design skill at `skills/ui-design-system-builder/SKILL.md` is the reusable execution contract for this gate.

## Companion development skills

Use the focused skills already available in the build environment when the task matches them:

- `apple-design` for Apple interaction patterns, fluid gestures, materials, typography, and reduced-motion behavior;
- `emil-design-eng` for design-engineering craft, hierarchy, component quality, and interaction feedback;
- `animate` or `animate-expo` when motion is central to a web or Expo experience;
- `react-native-best-practices` and `vercel-react-native-skills` for React Native/Expo performance and architecture;
- `react-native-testing` for React Native Testing Library coverage;
- `web-perf` for browser performance measurement and Core Web Vitals.

These skills complement the registry; they do not replace the platform's design system or the component-reuse rules.
