<%*
// create-project.md — project-creation macro for lithos-loom.
//
// Install: copy this file (verbatim) into your vault's Templater
// Template Folder, then bind Obsidian's "Templater: Insert
// create-project" command to a hotkey. Full instructions and
// behaviour notes live in docs/macros/README.md.
//
// What it does:
//   1. Loads obsidian_sync config from Loom (for the projects_dir
//      that the daemon will write into — used to validate the
//      inserted wikilink path).
//   2. Opens a single Obsidian Modal with title + slug + tags +
//      description fields. Slug autofills from a slugified title
//      and re-derives on each title keystroke until the operator
//      manually edits the slug field.
//   3. Validates the slug client-side for instant feedback.
//   4. Writes the description body to a tmpfile (avoids shell-
//      escape pain on multiline content).
//   5. Shells to `lithos-loom project create --format json
//      --body-file <tmpfile>`.
//   6. Parses the JSON response and inserts a wikilink at cursor
//      pointing at the projected vault path.
//
// Why no autocomplete for existing tags: the project-context tag
// is appended automatically server-side; operator-supplied extras
// are free-form. Tag autocomplete needs an extra RPC (lithos_tags)
// which adds latency and surface area — defer until friction is
// surfaced during use.

const { execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { Modal, Setting, Notice } = tp.obsidian;

// Client-side slug validation. MUST match the regex in
// src/lithos_loom/cli/project.py:_SLUG_RE — if they drift, the
// macro will accept slugs the CLI rejects (or vice versa) and the
// operator gets a confusing error well after submit. Keep in
// lock-step on changes.
const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;

// Mirror of cli/project.py:_slugify. Same NFKD-fold + lowercase +
// alphanumeric-only contract; pure JS so the modal can update the
// slug field in real-time without shelling out per keystroke.
function slugify(value) {
  if (!value) return "";
  const folded = value.normalize("NFKD");
  // Drop non-ASCII after fold (matches the Python side's
  // encode("ascii", errors="ignore") + decode).
  const ascii = folded.replace(/[-￿]/g, "");
  const lower = ascii.toLowerCase();
  return lower.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

// 1. Load obsidian_sync config — we'll need projects_dir for the
//    wikilink target (the daemon writes the projection there).
let obsCfg;
try {
  obsCfg = JSON.parse(
    execFileSync("lithos-loom", ["obsidian-sync", "show", "--format", "json"], {
      encoding: "utf-8",
    })
  );
} catch (e) {
  const stderr = (e.stderr && e.stderr.toString()) || e.message;
  new Notice(`Failed to load Loom config:\n${stderr}`, 10000);
  return;
}

// 2. Custom Modal with all fields visible at once.
class CreateProjectModal extends Modal {
  constructor(app, defaultTitle, onSubmit) {
    super(app);
    this.result = {
      title: defaultTitle || "",
      slug: slugify(defaultTitle || ""),
      tags: "",
      body: "",
    };
    // While true, slug auto-tracks title. The operator can break
    // tracking by editing the slug field directly.
    this.slugTracksTitle = true;
    this.submitted = false;
    this.onSubmit = onSubmit;
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.createEl("h2", { text: "Create Lithos project" });

    let slugInput; // captured for live-update from the title field

    new Setting(contentEl).setName("Title").addText((t) => {
      t.setValue(this.result.title).onChange((v) => {
        this.result.title = v;
        if (this.slugTracksTitle && slugInput) {
          const s = slugify(v);
          this.result.slug = s;
          slugInput.value = s;
        }
      });
      setTimeout(() => t.inputEl.focus(), 0);
      t.inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          this.submit();
        }
      });
    });

    new Setting(contentEl)
      .setName("Slug")
      .setDesc("lowercase letters, digits, hyphens. Auto-derived from Title until you edit it.")
      .addText((t) => {
        slugInput = t.inputEl;
        t.setValue(this.result.slug).onChange((v) => {
          this.result.slug = v;
          this.slugTracksTitle = false;
        });
      });

    new Setting(contentEl)
      .setName("Tags (comma-separated, optional)")
      .setDesc("project-context is added automatically.")
      .addText((t) => {
        t.onChange((v) => (this.result.tags = v));
      });

    new Setting(contentEl)
      .setName("Description (optional)")
      .setDesc("Becomes the doc body. Multiline is fine.")
      .addTextArea((ta) => {
        ta.onChange((v) => (this.result.body = v));
        // Make the textarea a bit taller than the default single-line
        // height so multi-paragraph descriptions don't feel cramped.
        ta.inputEl.rows = 6;
        ta.inputEl.style.width = "100%";
      });

    new Setting(contentEl)
      .addButton((b) =>
        b
          .setButtonText("Create")
          .setCta()
          .onClick(() => this.submit())
      )
      .addButton((b) => b.setButtonText("Cancel").onClick(() => this.close()));
  }

  submit() {
    if (!this.result.title.trim()) {
      new Notice("Title is required", 3000);
      return;
    }
    if (!SLUG_RE.test(this.result.slug)) {
      new Notice(
        `Invalid slug ${JSON.stringify(this.result.slug)}: must match lowercase alphanumerics + hyphens, start+end alphanumeric`,
        5000
      );
      return;
    }
    this.submitted = true;
    this.close();
  }

  onClose() {
    this.contentEl.empty();
    this.onSubmit(this.submitted ? this.result : null);
  }
}

// 3. Open modal, await result.
const form = await new Promise((resolve) => {
  new CreateProjectModal(app, tp.file.selection() || "", resolve).open();
});
if (!form) return;

// 4. Write body to a tmpfile so multiline content doesn't go
//    through the shell. Created in the OS temp dir; cleaned up in
//    a finally so a thrown JSON.parse doesn't leak the file.
const tmpfile = path.join(
  os.tmpdir(),
  `lithos-loom-create-${Date.now()}-${process.pid}.md`
);
fs.writeFileSync(tmpfile, form.body, { encoding: "utf-8" });

// 5. Shell out.
const args = [
  "project",
  "create",
  "--format",
  "json",
  "--title",
  form.title,
  "--slug",
  form.slug,
  "--body-file",
  tmpfile,
];
if (form.tags) args.push("--tags", form.tags);

let response;
try {
  const stdout = execFileSync("lithos-loom", args, { encoding: "utf-8" });
  response = JSON.parse(stdout);
} catch (e) {
  const stderr = (e.stderr && e.stderr.toString().trim()) || e.message;
  new Notice(`lithos-loom project create failed:\n${stderr}`, 10000);
  return;
} finally {
  try {
    fs.unlinkSync(tmpfile);
  } catch (_) {
    /* best-effort cleanup */
  }
}

// 6. Insert wiki-link at cursor. The vault_path from the CLI is
//    absolute; Obsidian wikilinks resolve against the vault root,
//    so we trim the vault prefix.
//    We rely on the CLI's vault_path being inside the vault — if
//    the operator has somehow configured obsidian_sync.vault_path
//    to something other than this vault's root, the wikilink will
//    point at an unresolvable path, but that's a config bug, not
//    a macro bug.
const vaultRoot = obsCfg.vault_path.replace(/\/+$/, "") + "/";
let relPath = response.vault_path;
if (relPath.startsWith(vaultRoot)) {
  relPath = relPath.slice(vaultRoot.length);
}
// Strip .md for prettier wikilinks (Obsidian's default).
const linkTarget = relPath.replace(/\.md$/, "");
const safeTitle = form.title.replace(/[\[\]\|]/g, " ").replace(/\s+/g, " ").trim();
tR += `[[${linkTarget}|${safeTitle}]]\n`;
%>
