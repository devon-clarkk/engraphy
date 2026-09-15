// Preload — the only bridge between the sandboxed renderer and the main process.
// Exposes a minimal, channel-tagged IPC surface (no Node, no ipcRenderer leak).
// The renderer builds a per-view `host` object on top of this that is shaped like
// the VS Code webview's acquireVsCodeApi(), so the copied confirm.js / stats.js
// render code works with a tiny header change.

import { contextBridge, ipcRenderer } from 'electron';

type Handler = (msg: unknown) => void;
const subscribers = new Map<string, Set<Handler>>();

ipcRenderer.on('engraphy:msg', (_e, payload: { channel: string; msg: unknown }) => {
	const set = subscribers.get(payload.channel);
	if (set) {
		for (const cb of set) {
			try {
				cb(payload.msg);
			} catch {
				// a bad subscriber must not break delivery to the others
			}
		}
	}
});

contextBridge.exposeInMainWorld('engraphyIPC', {
	/**
	 * Platform tag, so the renderer can lay out around the custom title bar:
	 * Windows draws OS window controls as an overlay in the top-right, macOS
	 * puts traffic lights in the top-left, Linux keeps a native frame. This is
	 * a constant string, not a Node handle, so it leaks no capability.
	 */
	platform: process.platform,
	/** Fire-and-forget renderer → main (mirrors webview postMessage). */
	send(channel: string, msg: unknown): void {
		ipcRenderer.send('engraphy:msg', { channel, msg });
	},
	/** Request/response renderer → main → result. */
	invoke(channel: string, msg: unknown): Promise<unknown> {
		return ipcRenderer.invoke('engraphy:invoke', { channel, msg });
	},
	/** Subscribe to main → renderer pushes on a channel. Returns an unsubscribe fn. */
	subscribe(channel: string, cb: Handler): () => void {
		let set = subscribers.get(channel);
		if (!set) {
			set = new Set();
			subscribers.set(channel, set);
		}
		set.add(cb);
		return () => set!.delete(cb);
	},
	/** Open an external https URL in the OS browser (validated in main). */
	openExternal(url: string): void {
		void ipcRenderer.invoke('engraphy:openExternal', url);
	},
});
