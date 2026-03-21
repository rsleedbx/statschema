# Working with git submodules in this repo

This repo has one submodule:

| Path | Remote |
|------|--------|
| `.cursor/` | `https://github.com/rsleedbx/skills` |

A submodule is a separate git repo embedded inside the parent repo.  Each has its own commit history and must be pushed independently.

---

## Clone

```bash
git clone --recurse-submodules https://github.com/rsleedbx/statschema.git
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init
```

---

## Day-to-day workflow

### When you change files under `.cursor/`

```bash
# 1. Go into the submodule
cd .cursor

# 2. Check what branch is active
git branch

# 3. If HEAD is detached, switch to main
git checkout main

# 4. Stage and commit
git add -A
git commit -m "describe your change"

# 5. Push to the skills repo
git push origin main

# 6. Return to the parent repo
cd ..

# 7. Stage the updated submodule reference
git add .cursor

# 8. Commit the reference update
git commit -m "update .cursor submodule"

# 9. Push the parent repo
git push origin dev
```

### When you change files outside `.cursor/`

```bash
git add <files>
git commit -m "describe your change"
git push origin dev
```

---

## Pulling updates

```bash
# Pull parent repo and update all submodules in one step
git pull --recurse-submodules

# Or separately:
git pull
git submodule update --remote
```

---

## Check current state

```bash
# Shows parent repo changes + whether submodule has uncommitted content
git status

# Shows which commit each submodule is at
git submodule status
```

`git status` output for submodules:

| Symbol | Meaning |
|--------|---------|
| ` m .cursor` | Submodule has local changes not yet committed inside it |
| `+043efc9 .cursor` | Submodule is at a newer commit than the parent recorded |
| `-043efc9 .cursor` | Submodule is not initialised — run `git submodule update --init` |

---

## Current submodule state in this repo

The `.cursor` submodule defaults to **detached HEAD** when the parent repo is cloned.  Checkout `main` before making changes:

```bash
cd .cursor
git checkout main
```

---

## Committed but not yet pushed

If `git push` is rejected because the submodule commit doesn't exist on the remote yet, push the submodule first (step 5 above), then push the parent.

```bash
# Check how many commits are ahead of remote
cd .cursor && git log origin/main..HEAD --oneline
cd ..      && git log origin/dev..HEAD --oneline
```
