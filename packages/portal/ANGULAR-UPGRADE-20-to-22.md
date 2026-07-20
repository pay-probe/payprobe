# Portal Angular Upgrade: 20 → 22

Current: Angular **20.3.24** / CLI 20.3.27 / TypeScript 5.8
Target: Angular **22** (latest stable, released 2026-06-03)

Angular only supports upgrading **one major at a time**, and each `ng update` runs
migration schematics for that specific jump. Do not skip 21.

> Run all commands from `packages/portal/`. Node 22.22.3 (your version) satisfies
> Angular 22's requirement (Node 20.19+ / 22.12+ / 24+). ✅

---

## 0. First: undo the partial sandbox changes

An earlier in-app attempt left two artifacts that the sandbox couldn't clean
(it can't delete files in the mounted repo). Run these locally first:

```bash
# from repo root
rm -f .git/index.lock                      # stale lock from an interrupted ng update

# from packages/portal
rm -rf node_modules
npm install                                # restore a clean Angular 20 node_modules
git status                                 # should be clean; package.json is back to ^20
```

Confirm you're back to a working baseline before upgrading:

```bash
npm run build      # production build should succeed
```

---

## 1. Upgrade 20 → 21

```bash
git switch -c chore/angular-21
ng update @angular/core@21 @angular/cli@21
ng update @angular/cdk@21 @angular/material@21
```

Also bump the graph library (v8 is too old for 21; v12 supports 19/20/21):

```bash
npm install @swimlane/ngx-graph@^12.0.0
```

Then verify and commit:

```bash
npm run build
npm test -- --watch=false
git add -A && git commit -m "chore(portal): upgrade Angular 20 → 21"
```

Review the 20→21 breaking-change checklist at https://angular.dev/update-guide
(set "from 20.0, to 21.0").

---

## 2. Upgrade 21 → 22  ⚠️ has a blocker

```bash
git switch -c chore/angular-22
ng update @angular/core@22 @angular/cli@22
ng update @angular/cdk@22 @angular/material@22
```

### Blocker: `@swimlane/ngx-graph`
Its newest release (12.0.0) declares peer support only for **Angular 19/20/21**,
*not* 22. This library drives the Test Constructor flow editor. Options:

1. **Wait** for an ngx-graph release that supports Angular 22 (check
   https://www.npmjs.com/package/@swimlane/ngx-graph before doing step 2).
2. **Force it** and test thoroughly — peer-dep mismatch often still runs:
   ```bash
   npm install --legacy-peer-deps
   ```
   Then manually exercise the flow/graph editor for runtime breakage.
3. **Replace ngx-graph** with a maintained alternative (e.g. a direct d3-dagre
   integration, or `@foblex/flow`) if it stays incompatible.

`monaco-editor` is framework-agnostic and needs no Angular-version change.

Verify and commit:

```bash
npm run build
npm test -- --watch=false
git add -A && git commit -m "chore(portal): upgrade Angular 21 → 22"
```

---

## Notes / risk summary

- **ngx-graph** is the main risk — no Angular 22 support yet. Decide between
  waiting, force-installing, or replacing before committing to step 2.
- TypeScript is bumped automatically by `ng update` (21 → TS 5.9; 22 → its
  supported range). Don't pin it manually.
- `@angular/material` 21/22 may run token/theming migrations — review the diff
  on your custom theme (you use CSS-var + ThemeService).
- Re-run the e2e suite (`npm run e2e`) after step 2 as a final gate.
- Official guide for the exact breaking changes per step:
  https://angular.dev/update-guide
