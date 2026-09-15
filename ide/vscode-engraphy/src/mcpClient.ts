// Typed Engraphy client. Speaks MCP Streamable HTTP (via the official SDK) to
// the server's /mcp endpoint with the bearer token from settings, and hits the
// unauthenticated /healthz for the connection indicator.
//
// The server exposes only MCP tools over /mcp (search/traverse/get/write/
// resolve_duplicate/inbox_review/briefing/...) — there is no REST read API — so
// every read/act goes through callTool. Result parsing/arg building live in the
// pure toolResult module.

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import {
	buildGetArgs,
	buildInboxDiscardArgs,
	buildInboxListArgs,
	buildInboxPromoteArgs,
	buildPendingListArgs,
	buildResolveDuplicateArgs,
	buildSearchArgs,
	buildTraverseArgs,
	normalizeServerUrl,
	parseToolResult,
	type PromoteOpts,
	type RawToolResult,
	type SearchOpts,
	type TraverseOpts,
} from './toolResult';
import { parseStatsResult, type StatsResult } from './statsModel';

export interface EngraphyConnection {
	serverUrl: string;
	token: string;
	space: string;
}

export interface HealthInfo {
	status: string;
	version?: string;
	schema_version?: string;
	spaces?: number;
	embedding_model?: string;
}

export class EngraphyClient {
	private client: Client | undefined;
	private transport: StreamableHTTPClientTransport | undefined;
	private connectionKey = '';
	/**
	 * The connect that is currently in flight, keyed by the same url+token string
	 * as `connectionKey`. Without this, the several callers that fire at startup
	 * (status bar, confirm panel, stats panel) each raced through the
	 * `this.client === undefined` check, each built a transport, and only the last
	 * assignment survived: the losers were never closed, so every activation
	 * leaked MCP sessions on the server.
	 */
	private connecting: Promise<Client> | undefined;
	private connectingKey = '';

	constructor(
		private readonly getConnection: () => EngraphyConnection,
		private readonly clientVersion: string
	) {}

	/** Connect lazily; reconnect when the URL or token changed since last connect. */
	private async ensureConnected(): Promise<Client> {
		const conn = this.getConnection();
		if (!conn.serverUrl) {
			throw new Error('engraphy.serverUrl is not set.');
		}
		// Normalize to a trailing-slash path before the transport touches it, so a
		// `/mcp` config never eats the 307-redirect stall (mcp-trailing-slash-307).
		const serverUrl = normalizeServerUrl(conn.serverUrl);
		const key = `${serverUrl} ${conn.token}`;
		if (this.client && key === this.connectionKey) {
			return this.client;
		}
		// Concurrent callers share ONE connect. Keyed so that a settings change
		// mid-connect starts a fresh one rather than handing back a client built
		// with the old token.
		if (this.connecting && key === this.connectingKey) {
			return this.connecting;
		}
		const attempt = this.openSession(serverUrl, conn.token, key);
		this.connecting = attempt;
		this.connectingKey = key;
		try {
			return await attempt;
		} finally {
			// Only clear if this attempt is still the current one; a newer connect
			// started under a different key owns the slot now.
			if (this.connecting === attempt) {
				this.connecting = undefined;
				this.connectingKey = '';
			}
		}
	}

	/** Build and connect one transport+client pair, replacing any current one. */
	private async openSession(
		serverUrl: string,
		token: string,
		key: string
	): Promise<Client> {
		await this.close({ keepInFlight: true });

		const headers: Record<string, string> = {};
		if (token) {
			headers['Authorization'] = `Bearer ${token}`;
		}
		const transport = new StreamableHTTPClientTransport(new URL(serverUrl), {
			requestInit: { headers },
		});
		const client = new Client(
			{ name: 'engraphy-vscode', version: this.clientVersion },
			{ capabilities: {} }
		);
		try {
			await client.connect(transport);
		} catch (e) {
			// A failed connect must not leave a half-open transport behind either.
			try {
				await transport.close();
			} catch {
				// best-effort teardown
			}
			throw e;
		}

		this.client = client;
		this.transport = transport;
		this.connectionKey = key;
		return client;
	}

	/** Force the next call to reconnect (e.g. after settings change). */
	async reconnect(): Promise<void> {
		await this.close();
	}

	async close(opts?: { keepInFlight?: boolean }): Promise<void> {
		const c = this.client;
		this.client = undefined;
		this.transport = undefined;
		this.connectionKey = '';
		if (!opts?.keepInFlight) {
			// An external close (reconnect, dispose) also abandons any connect still
			// in flight, so the next call builds a fresh one.
			this.connecting = undefined;
			this.connectingKey = '';
		}
		try {
			await c?.close();
		} catch {
			// best-effort teardown
		}
	}

	private async call(name: string, args: Record<string, unknown>): Promise<unknown> {
		const client = await this.ensureConnected();
		const res = (await client.callTool({ name, arguments: args })) as RawToolResult;
		return parseToolResult(res);
	}

	// ---- typed tool methods -------------------------------------------------

	search(opts: SearchOpts): Promise<unknown> {
		return this.call('search', buildSearchArgs(opts));
	}
	traverse(opts: TraverseOpts): Promise<unknown> {
		return this.call('traverse', buildTraverseArgs(opts));
	}
	get(ids: string[]): Promise<unknown> {
		return this.call('get', buildGetArgs(ids));
	}
	briefing(scope: string, hint?: string): Promise<unknown> {
		const args: Record<string, unknown> = { scope };
		if (hint) {
			args.hint = hint;
		}
		return this.call('briefing', args);
	}
	scopeList(): Promise<unknown> {
		return this.call('scope_list', {});
	}
	/** Usage/value stats for the value dashboard. Omitted args take server defaults. */
	async stats(rangeDays?: number, groupBy?: 'space' | 'user'): Promise<StatsResult> {
		const args: Record<string, unknown> = {};
		if (rangeDays !== undefined) {
			args.range_days = rangeDays;
		}
		if (groupBy !== undefined) {
			args.group_by = groupBy;
		}
		return parseStatsResult(await this.call('stats', args));
	}
	inboxList(limit?: number, offset?: number): Promise<unknown> {
		return this.call('inbox_review', buildInboxListArgs(limit, offset));
	}
	pendingList(limit?: number, offset?: number): Promise<unknown> {
		return this.call('pending_list', buildPendingListArgs(limit, offset));
	}
	inboxPromote(opts: PromoteOpts): Promise<unknown> {
		return this.call('inbox_review', buildInboxPromoteArgs(opts));
	}
	inboxDiscard(id: string): Promise<unknown> {
		return this.call('inbox_review', buildInboxDiscardArgs(id));
	}
	resolveDuplicate(
		pendingId: string,
		resolution: 'distinct' | 'merge',
		mergeInto?: string
	): Promise<unknown> {
		return this.call('resolve_duplicate', buildResolveDuplicateArgs(pendingId, resolution, mergeInto));
	}

	/** GET /healthz (unauthenticated) — derived from the MCP URL's origin. */
	async health(): Promise<HealthInfo> {
		const conn = this.getConnection();
		const healthUrl = new URL('/healthz', conn.serverUrl).toString();
		const resp = await fetch(healthUrl, { method: 'GET' });
		if (!resp.ok) {
			throw new Error(`healthz ${resp.status}`);
		}
		return (await resp.json()) as HealthInfo;
	}
}
