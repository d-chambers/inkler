# Inkler

An agent and a person, working in the **same existing Inkscape window**.

Ask your agent to “turn the black square into a cat” or “draw an arrow pointing to
the dog.” Inkler gives the agent the live document's selection, object IDs and
geometry, a rendered preview, and structured SVG editing tools. The agent supplies
the artwork. Changes go through a normal Inkscape extension so you can undo them.

**Experimental Linux prototype.** Requires a normal desktop Inkscape installation
(developed with Ubuntu 26.04 and Inkscape 1.4.3). Exactly one Inkscape window/document
must be open. It attaches to that window; it never launches or reloads an editor.
Preview rendering uses a separate *headless* Inkscape process.

## Install

No pip, Conda, downloaded MCP framework, network service, or administrator access is
needed on an Ubuntu desktop with Inkscape installed. Uses system Python 3,
PyGObject/Gio, and the inkex/lxml libraries supplied with Inkscape.

```bash
git clone https://github.com/d-chambers/inkler.git
cd inkler
./inkler install
```

Save your drawing and restart Inkscape once to register the extension. Keep one
document open, then select **Extensions → Inkler → Connect Agent**. The small panel
shows a local connection code and a **Copy instructions** button. Paste those
instructions into your local coding agent: they point to the installed skill and
tell the agent how to connect. You do not need to configure MCP for this workflow.

For terminal diagnostics:

```bash
./inkler status
./inkler inspect
./inkler preview
```

The agent runs `inkler connect INKLER-CODE` once, then invokes the extension through
the local session D-Bus. Opening Connect Agent creates a fresh code and disconnects
previous attachments. Codes are local document selectors, not a security boundary
against other programs running as your OS account. They become stale when the
document closes or Inkscape restarts. Creating a code requires an idle bridge.

Inkler's executable uses `/usr/bin/python3`, so activating a Conda environment does
not replace the interpreter. The extension is executable with the same system
Python shebang; installation does not change Inkscape's interpreter preferences.
Other Linux distributions may have different library paths and need adaptation.

## Optional MCP connection

Run Inkler as a local stdio MCP server. For clients accepting `mcpServers` JSON:

```json
{
  "mcpServers": {
    "inkler": {
      "command": "/absolute/path/to/inkler/inkler",
      "args": ["mcp"]
    }
  }
}
```

Use your actual checkout path. On the development machine that is
`/home/derrick/Gits/inkler/inkler`. Start the agent in the same desktop login session
as Inkscape. It needs access to the session bus and the shared private Inkler cache.
A container, remote session or sandbox that blocks D-Bus cannot attach just because
the executable is installed. The MCP client may need a restart after configuration.

The five tools are `inkler_connect`, `inkler_status`, `inkler_inspect`,
`inkler_preview`, and `inkler_edit`. Previews are returned as image content. An agent with
shell access can use the CLI instead; it does not need MCP.

If you created a connection code in the panel, the agent calls `inkler_connect`
with that code before invoking drawing tools. No shell connection command is
needed when using MCP. The current
prototype shares one local attachment among clients in that account; concurrent
operations are serialized.

For Codex, register the installed server once from your normal desktop terminal:

```bash
codex mcp add inkler -- "$HOME/.config/inkscape/extensions/inkler/inkler" mcp
```

Reload your agent client/tools after registration. The server process must have
desktop session bus access. MCP and D-Bus are different layers:
`agent → local stdio MCP server → D-Bus → Inkscape extension → existing document`.
The server has an MCP initialize handshake, tool discovery, tool calls and image
responses; it does not need a network listener or HTTP service. A restricted shell
can fail to reach D-Bus even when an appropriately authorized client-launched MCP
process can. That behavior depends on the client and its managed policy.

Suggested first prompt:

> Connect to Inkler. Inspect my selection and preview the canvas. Replace the
> selected black square with a simple vector cat, then add an arrow pointing to the
> dog. Keep the surrounding drawing and show me the result.

Start with a copy of a drawing until live behavior has been verified on your setup.

## Editing protocol

Inspect first. The result includes a `document` token, a `revision`, selected object
IDs, and object attributes. Bboxes are `[x, y, width, height]` in **SVG user units**
in document coordinates. They describe geometry, omit stroke, and may approximate
text; the preview is the visual check. `--ids square dog` limits inspection output.

Send a batch of at most 100 operations, together with the inspection tokens:

```json
{
  "document": "COPY_FROM_INSPECT",
  "revision": "COPY_FROM_INSPECT",
  "operations": [
    {
      "op": "replace",
      "id": "square",
      "svg": "<g id=\"cat\"><path d=\"M40 110 L40 40 L65 60 L95 60 L120 40 L120 110 Z\" fill=\"black\"/></g>"
    },
    {
      "op": "add",
      "svg": "<path id=\"arrow\" d=\"M130 150 L200 110 M187 111 L200 110 L195 122\" fill=\"none\" stroke=\"red\" stroke-width=\"3\"/>"
    }
  ]
}
```

Save the JSON as `edit.json` and run `./inkler edit edit.json`, or send that object
to the `inkler_edit` MCP tool. Example coordinates match `examples/demo.svg` only;
the agent should draw artwork appropriate to your actual document.

Supported operations:

| Operation | Fields | Behavior |
|---|---|---|
| `add` | `svg`, optional `parent` ID | Append elements to a container, or to the document root. |
| `replace` | `id`, `svg` | Replace an object at its existing stacking position. |
| `remove` | `id` | Remove an object. |
| `set` | `id`, `attrs` | Set attributes; a null value removes an attribute. |
| `text` | `id`, `text` | Replace the contents of a text element, discarding its child spans. |

SVG fragments contain elements or groups, **without an outer `<svg>` wrapper**.
Paths, shapes, text, groups, gradients, local markers, masks, clipping and patterns
are supported. Every new meaningful object should have a unique, simple ID.

`replace` uses the old object's **parent coordinate system**. It preserves stacking
position but does not inherit the removed object's transform or style. Use the
inspection's parent transforms when positioning replacement artwork. Adding to the
root uses document coordinates. Existing objects without IDs cannot be targeted
directly in this prototype; their identified ancestor can be replaced instead.

Each edit batch is validated against a copy before output, so a bad operation does
not apply half the batch. If you change the drawing after inspection, Inkler rejects
the stale revision. Zoom and viewport changes are excluded from this check. After
an edit, inspect again and preview rather than assuming the old token remains valid.

**You control saving.** Inkler does not save over your SVG or erase undo history.
Use Inkscape's Undo normally. It is not a collaborative CRDT: avoid changing the
same object while an extension operation is in flight.

## Security and trust

- Small local source tree; no self-updater, telemetry, network listener, package
  downloader, or arbitrary Python/shell execution tool.
- Agent edits allow structured vector content. Scripts, event handlers, foreign
  objects, external references and embedded images are rejected in new fragments.
  Existing drawing resources are not sanitized; the preview renderer still processes
  the content of the user's document. This is **not a sandbox for hostile SVG files**.
- Explicit installation only, into `~/.config/inkscape/extensions/inkler`. A hash
  manifest detects locally modified installed files before updates or removal.
- Requests, results and snapshots live in a mode-700 cache with mode-600 files.
  Cross-process locks, per-request IDs, atomic result writes and a single pending
  request prevent clients overwriting each other's work.
- A timeout has an unknown outcome. Inkler retains pending state and refuses new
  requests instead of automatically retrying an edit. Inspect your canvas first.
- The trust boundary is your OS user account. Processes running as you can modify
  the extension or its files. No claim of protection against a compromised account.

Snapshots/previews remain in `~/.cache/inkler/job-*` until you delete them. They can
contain confidential artwork. No automatic cleanup deletes old user-visible results.

If a dialog blocks a request, dismiss it and allow that request to finish. If it
remains stuck, quit and reopen Inkscape, then run:

```bash
./inkler reset --after-restart
```

Reset verifies that the Inkscape D-Bus owner changed before clearing pending work.
It refuses to clear it when a restart cannot be verified. Do not manually replay a
timed-out edit without checking whether it already applied.

To uninstall, run `./inkler uninstall`, then restart Inkscape. Remove the MCP client
configuration separately. Cache files remain for manual deletion while Inkler is
idle. Modified installation files require manual inspection instead of silent removal.

## Tests and limits

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

Tests cover edit validation, stale revisions, atomic batches, SVG content restrictions,
IPC permissions/locking, extension subprocess input/output, geometry, the INX schema,
and the stdio MCP handshake. A fixture demonstrates the square-to-cat and arrow edits.
Headless rendering is tested with the installed Inkscape binary.

Live D-Bus attachment and GUI undo still require a manual desktop smoke test. The
development agent's environment blocks desktop D-Bus, so the initial implementation
does not claim these have been verified live. Save a disposable fixture, open it as
the only document, inspect, preview, edit, and verify Undo restores it.

Not included: multiple editor windows, Windows/macOS, cloud collaboration, batch
conversion, arbitrary installed-extension execution, native alignment/boolean-action
wrappers, or an embedded chat panel. This tool supplies agent access, not an AI model.

## Related projects

These existing bridges informed the feasibility review and are alternatives with
broader functionality. Inkler is a small independent implementation, not a fork:

- [aravindev/inkscape_mcp](https://github.com/aravindev/inkscape_mcp) — live and headless
  operation, extensive Inkscape actions and extension integration.
- [Shriinivas/inkmcp](https://github.com/Shriinivas/inkmcp) — Linux live-document
  integration using D-Bus and an Inkscape extension.
- [jaags-dev/inkscape-mcp](https://github.com/jaags-dev/inkscape-mcp) — SVG editing,
  export and browser preview through MCP.

Built on Inkscape/inkex, lxml, PyGObject/Gio and Python supplied by the OS. See the
[Inkscape extension documentation](https://inkscape.gitlab.io/extensions/documentation/)
and [MCP specification](https://modelcontextprotocol.io/specification/2025-11-25).

MIT license; see LICENSE.
