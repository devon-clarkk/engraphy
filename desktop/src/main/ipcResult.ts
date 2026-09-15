// The uniform request/response contract for `engraphy:invoke`.
//
// WHY: `ipcMain.handle` turns a thrown error into a REJECTED promise, but
// main.ts's wrapper catches everything and RESOLVES `{ok:false, error}` instead
// (so a renderer never eats an unhandled rejection). That means a renderer
// `try/catch` around `host.invoke(...)` never fires, and any view that read
// `res.nodes` straight off the result silently rendered "no results" for a dead
// server or a rejected token. Every handler now returns this discriminated
// shape and every caller branches on `ok` before touching the payload.
//
// Pure: no electron, no DOM. Covered by scripts/test-client.js.

import { describeError, type DescribedError } from './connection';

export interface InvokeFailure {
	ok: false;
	error: DescribedError;
}

export type InvokeResult<T> = ({ ok: true } & T) | InvokeFailure;

/** Wrap a successful payload. */
export function ok<T extends object>(payload: T): { ok: true } & T {
	return { ok: true, ...payload };
}

/** Wrap a thrown value as a classified failure. */
export function fail(e: unknown, host = ''): InvokeFailure {
	return { ok: false, error: describeError(e, host) };
}

/** Narrowing helper, used by tests and by main. */
export function isFailure(r: unknown): r is InvokeFailure {
	return !!r && typeof r === 'object' && (r as { ok?: unknown }).ok === false;
}
