// Memory explorer TreeView: search box → results → expand a node to traverse to
// its linked neighbors. Backed by the Engraphy client (search / traverse / get).

import * as vscode from 'vscode';
import { EngraphyClient } from './mcpClient';

interface NodeRef {
	id: string;
	title: string;
	type: string;
	scope: string;
}

/** Best-effort extraction of node refs from a search or traverse envelope. */
function nodesFromSearch(result: unknown): NodeRef[] {
	const r = result as { results?: Array<{ node?: Record<string, unknown> }> };
	return (r.results ?? [])
		.map((x) => x.node)
		.filter((n): n is Record<string, unknown> => !!n)
		.map(toRef);
}
function nodesFromTraverse(result: unknown): NodeRef[] {
	const r = result as { nodes?: Array<Record<string, unknown>> };
	return (r.nodes ?? []).map(toRef);
}
function toRef(n: Record<string, unknown>): NodeRef {
	return {
		id: String(n.id ?? ''),
		title: String(n.title ?? '(untitled)'),
		type: String(n.type ?? '?'),
		scope: String(n.scope ?? '?'),
	};
}

export class ExplorerNode extends vscode.TreeItem {
	constructor(public readonly ref: NodeRef) {
		super(ref.title, vscode.TreeItemCollapsibleState.Collapsed);
		this.description = `${ref.type} · ${ref.scope}`;
		this.tooltip = `${ref.title}\n${ref.type} · scope ${ref.scope}\nid ${ref.id}`;
		this.contextValue = 'engraphyNode';
		this.iconPath = new vscode.ThemeIcon('circle-filled');
		this.command = {
			command: 'engraphy.viewNodeDetails',
			title: 'View details',
			arguments: [ref.id],
		};
	}
}

class InfoItem extends vscode.TreeItem {
	constructor(label: string, icon = 'info') {
		super(label, vscode.TreeItemCollapsibleState.None);
		this.iconPath = new vscode.ThemeIcon(icon);
		this.contextValue = 'engraphyInfo';
	}
}

export class ExplorerProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
	private readonly _onDidChange = new vscode.EventEmitter<void>();
	readonly onDidChangeTreeData = this._onDidChange.event;

	private results: NodeRef[] | undefined;
	private lastQuery = '';

	constructor(
		private readonly client: EngraphyClient,
		private readonly log: vscode.OutputChannel
	) {}

	setResults(query: string, result: unknown): void {
		this.lastQuery = query;
		this.results = nodesFromSearch(result);
		this._onDidChange.fire();
	}

	clear(): void {
		this.results = undefined;
		this._onDidChange.fire();
	}
	refresh(): void {
		this._onDidChange.fire();
	}

	getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
		return element;
	}

	async getChildren(element?: vscode.TreeItem): Promise<vscode.TreeItem[]> {
		if (!element) {
			if (this.results === undefined) {
				const hint = new InfoItem('Run "Engraphy: Search…" to explore memory', 'search');
				hint.command = { command: 'engraphy.search', title: 'Search' };
				return [hint];
			}
			if (this.results.length === 0) {
				return [new InfoItem(`No results for "${this.lastQuery}"`, 'circle-slash')];
			}
			return this.results.map((r) => new ExplorerNode(r));
		}

		if (element instanceof ExplorerNode) {
			// Expand → neighbors via a depth-1 traverse in both directions.
			try {
				const res = await this.client.traverse({
					startId: element.ref.id,
					direction: 'both',
					maxDepth: 1,
					detail: 'summary',
				});
				const neighbors = nodesFromTraverse(res).filter((n) => n.id !== element.ref.id);
				if (neighbors.length === 0) {
					return [new InfoItem('No linked nodes', 'circle-slash')];
				}
				return neighbors.map((r) => new ExplorerNode(r));
			} catch (e) {
				this.log.appendLine(`traverse failed for ${element.ref.id}: ${String(e)}`);
				return [new InfoItem(`Traverse failed: ${String(e)}`, 'error')];
			}
		}
		return [];
	}
}
