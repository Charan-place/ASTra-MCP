/**
 * graphTreeView.ts
 *
 * Minimal sidebar TreeView: root = top-level symbols in the currently
 * active file (by caller count, as an importance proxy), expandable one
 * level to show that symbol's callees. This is deliberately simple —
 * a flat/2-level tree, not a full graph explorer like the D3 dashboard.
 */

import * as vscode from 'vscode';
import { AstraClient, AstraNode } from './astraClient';

type TreeElement = SymbolItem | CalleeItem | MessageItem;

class MessageItem extends vscode.TreeItem {
  constructor(message: string) {
    super(message, vscode.TreeItemCollapsibleState.None);
    this.contextValue = 'astraMessage';
  }
}

class SymbolItem extends vscode.TreeItem {
  constructor(public readonly node: AstraNode, public readonly callerCount: number, public readonly calleeCount: number) {
    super(node.name, calleeCount > 0 ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    this.description = `${node.type} · ${callerCount} callers · ${calleeCount} callees`;
    this.tooltip = `${node.file}:${node.line_start ?? node.line ?? ''}`;
    this.iconPath = new vscode.ThemeIcon(node.type === 'class' ? 'symbol-class' : 'symbol-function');
    this.contextValue = 'astraSymbol';
    this.command = {
      command: 'astra.revealSymbol',
      title: 'Reveal symbol',
      arguments: [node],
    };
  }
}

class CalleeItem extends vscode.TreeItem {
  constructor(public readonly node: AstraNode) {
    super(node.name, vscode.TreeItemCollapsibleState.None);
    this.description = node.file;
    this.iconPath = new vscode.ThemeIcon('arrow-small-right');
    this.contextValue = 'astraCallee';
    this.command = {
      command: 'astra.revealSymbol',
      title: 'Reveal symbol',
      arguments: [node],
    };
  }
}

export class AstraGraphTreeProvider implements vscode.TreeDataProvider<TreeElement> {
  private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<TreeElement | undefined | void>();
  readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;

  constructor(private readonly client: AstraClient) {
    vscode.window.onDidChangeActiveTextEditor(() => this.refresh());
  }

  refresh(): void {
    this.onDidChangeTreeDataEmitter.fire();
  }

  getTreeItem(element: TreeElement): vscode.TreeItem {
    return element;
  }

  async getChildren(element?: TreeElement): Promise<TreeElement[]> {
    if (element instanceof SymbolItem) {
      const detail = await this.client.getNodeDetail(element.node.id);
      if (!detail) {
        return [];
      }
      return detail.callees.map((n) => new CalleeItem(n));
    }
    if (element) {
      return []; // CalleeItem / MessageItem are leaves
    }

    // Root level: top symbols of the active editor's file.
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      return [new MessageItem('Open a file to see its call graph.')];
    }

    const reachable = await this.client.ping();
    if (!reachable) {
      return [new MessageItem('ASTra daemon/dashboard not reachable. Run "astra daemon start" or "astra dashboard".')];
    }

    const nodes = await this.client.getSymbolsForFile(editor.document.uri.fsPath);
    const callable = nodes.filter((n) => n.type !== 'file');
    if (callable.length === 0) {
      return [new MessageItem('No indexed symbols found for this file.')];
    }

    const withCounts = await Promise.all(
      callable.map(async (node) => {
        const detail = await this.client.getNodeDetail(node.id);
        return {
          node,
          callerCount: detail?.callers.length ?? 0,
          calleeCount: detail?.callees.length ?? 0,
        };
      }),
    );

    withCounts.sort((a, b) => b.callerCount - a.callerCount);

    return withCounts.map((w) => new SymbolItem(w.node, w.callerCount, w.calleeCount));
  }
}
