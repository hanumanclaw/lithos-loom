<%*
// capture-task.md — capture macro for lithos-loom.
//
// Install: copy this file (verbatim) into your vault's Templater
// Template Folder, then bind Obsidian's "Templater: Insert
// capture-task" command to a hotkey. Full instructions and
// behaviour notes live in docs/macros/README.md.
//
// What it does:
//   1. Loads project list via `lithos-loom project list --format json`
//   2. Opens a single Obsidian Modal with all six fields visible
//   3. Calls `lithos-loom task create --no-insert ...`
//   4. Inserts a wiki-link at cursor pointing at _lithos/tasks.md
//      (the daemon's projection writes the canonical task line into
//      that file independently — we never duplicate the line here)

const { execSync, execFileSync } = require("child_process");
// `tp.obsidian` is Templater's seam for Obsidian's API classes
// (Modal, Setting, Notice). `require("obsidian")` does NOT work
// in a Templater scriptlet — the `obsidian` module is only
// resolvable from plugin code, not from the Eta exec context.
const { Modal, Setting, Notice } = tp.obsidian;

// 1. Load project list AND obsidian_sync config from Loom. The
//    tasks_file path is operator-configurable; hardcoding the
//    default would break the wikilink on hosts that customise it.
let projects;
let tasksFile;
try {
  projects = JSON.parse(
    execSync("lithos-loom project list --format json", { encoding: "utf-8" })
  );
  const obsCfg = JSON.parse(
    execSync("lithos-loom obsidian-sync show --format json", {
      encoding: "utf-8",
    })
  );
  tasksFile = obsCfg.tasks_file;
} catch (e) {
  const stderr = (e.stderr && e.stderr.toString()) || e.message;
  new Notice(`Failed to load Loom config:\n${stderr}`, 10000);
  return;
}
if (!projects.length) {
  new Notice(
    "No projects found in Lithos. Create one first (create-project macro or `lithos-loom project create`).",
    10000
  );
  return;
}

// 2. Custom Modal with all fields in one dialog.
class CaptureModal extends Modal {
  constructor(app, projects, defaultTitle, onSubmit) {
    super(app);
    this.projects = projects;
    this.result = {
      project: projects[0],
      title: defaultTitle || "",
      brief: "",
      scheduled: "",
      priority: "",
      tags: "",
    };
    this.submitted = false;
    this.onSubmit = onSubmit;
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.createEl("h2", { text: "Capture Lithos task" });

    new Setting(contentEl).setName("Project").addDropdown((dd) => {
      this.projects.forEach((p) => dd.addOption(p, p));
      dd.setValue(this.result.project).onChange((v) => (this.result.project = v));
    });

    new Setting(contentEl).setName("Title").addText((t) => {
      t.setValue(this.result.title).onChange((v) => (this.result.title = v));
      // Auto-focus title after render so Tab order starts here.
      setTimeout(() => t.inputEl.focus(), 0);
      // Enter-to-submit when the title field has focus.
      t.inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          this.submit();
        }
      });
    });

    new Setting(contentEl).setName("Brief (optional)").addTextArea((ta) => {
      ta.onChange((v) => (this.result.brief = v));
    });

    new Setting(contentEl).setName("Scheduled (optional)").then((s) => {
      // Native HTML5 date picker — Obsidian runs on Chromium so
      // `<input type="date">` gives the same calendar widget as the
      // Tasks plugin's date control. The browser-emitted value is
      // already `YYYY-MM-DD`, matching what `lithos-loom task
      // create --scheduled` expects. Operators can click the icon
      // for the picker or type digits directly.
      const input = s.controlEl.createEl("input", { type: "date" });
      input.addEventListener(
        "change",
        () => (this.result.scheduled = input.value)
      );
    });

    new Setting(contentEl).setName("Priority").addDropdown((dd) => {
      ["", "highest", "high", "medium", "low", "lowest"].forEach((p) =>
        dd.addOption(p, p || "(none)")
      );
      dd.onChange((v) => (this.result.priority = v));
    });

    new Setting(contentEl)
      .setName("Tags (comma-separated, optional)")
      .addText((t) => {
        t.onChange((v) => (this.result.tags = v));
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
  new CaptureModal(app, projects, tp.file.selection() || "", resolve).open();
});
if (!form) return;

// 4. Build argv. --no-insert returns just the task_id; the daemon's
//    projection subscription writes the canonical line into
//    _lithos/tasks.md independently when Lithos broadcasts
//    task.created. We never insert the task line here — that would
//    create a stale duplicate.
const args = [
  "task", "create", "--no-insert",
  "--project", form.project,
  "--title", form.title,
];
if (form.brief)     args.push("--brief", form.brief);
if (form.scheduled) args.push("--scheduled", form.scheduled);
if (form.priority)  args.push("--priority", form.priority);
if (form.tags)      args.push("--tags", form.tags);

// 5. Shell out; capture task_id from stdout.
let taskId;
try {
  taskId = execFileSync("lithos-loom", args, { encoding: "utf-8" }).trim();
} catch (e) {
  const stderr = (e.stderr && e.stderr.toString().trim()) || e.message;
  new Notice(`lithos-loom task create failed:\n${stderr}`, 10000);
  return;
}

// 6. Sanitise title for wikilink display text — Obsidian wikilink
//    syntax breaks on [ ] | and newlines.
const safeTitle = form.title.replace(/[\[\]\|]/g, " ").replace(/\s+/g, " ").trim();

// 7. Insert wiki-link at cursor. Title is the clickable bit; the
//    trailing 🆔 lithos:<id> is greppable from anywhere in the vault.
//    `tasksFile` came from `lithos-loom obsidian-sync show` so it
//    respects any operator-customised `obsidian_sync.tasks_file`.
tR += `[[${tasksFile}|${safeTitle}]] 🆔 lithos:${taskId}\n`;
%>
