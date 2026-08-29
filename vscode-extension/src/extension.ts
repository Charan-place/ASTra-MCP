/**
 * extension.ts — activation entry point.
 *
 * Wires up:
 *  - AstraClient (talks to the local ASTra daemon socket / dashboard HTTP API)
 *  - CodeLens provider (inline caller/callee/importance summaries)
 *  - Hover provider (rich markdown symbol details + drift warnings)
 *  - Sidebar TreeView (simple call graph rooted at the active file)
 *  - A status bar item reflecting daemon/dashboard reachability
 */

import * as vscode from 'vscode';
import { AstraClient, AstraNode } from './astraClient';
import { AstraCodeLensProvider } from './codeLensProvider';
import { AstraHoverProvider } from './hoverProvider';
import { AstraGraphTreeProvider } from './graphTreeView';

const SUPPORTED_LANGUAGES = [
  'python',
  'javascript',
  'typescript',
  'javascriptreact',
  'typescriptreact',
  // Support for these is landing in a parallel ASTra indexer workstream;
  // listed here so the extension "just works" once daemon-side support ships.
  'go',
  'rust',
  'java',
];

export function activate(context: vscode.ExtensionContext): void {
  const client = new AstraClient();

  const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.command = 'astra.showStatus';
  statusBarItem.text = '$(sync~spin) ASTra';
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  const setStatusBar = (reachable: boolean) => {
    if (reachable) {
      statusBarItem.text = '$(check) ASTra';
      statusBarItem.tooltip = 'ASTra daemon/dashboard reachable';
      statusBarItem.backgroundColor = undefined;
    } else {
      statusBarItem.text = '$(warning) ASTra offline';
      statusBarItem.tooltip =
        'ASTra daemon/dashboard not reachable.\nRun "astra daemon start" and/or "astra dashboard" in your project root.';
      statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
    }
  };

  context.subscriptions.push(client.onReachabilityChanged(setStatusBar));
  // Initial probe (fire-and-forget — must never throw/crash activation).
  client.ping().then(setStatusBar).catch(() => setStatusBar(false));

  // ── CodeLens ───────────────────────────────────────────────────────────
  const codeLensProvider = new AstraCodeLensProvider(client);
  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider(
      SUPPORTED_LANGUAGES.map((language) => ({ language, scheme: 'file' })),
      codeLensProvider,
    ),
  );

  // ── Hover ──────────────────────────────────────────────────────────────
  const hoverProvider = new AstraHoverProvider(client);
  context.subscriptions.push(
    vscode.languages.registerHoverProvider(
      SUPPORTED_LANGUAGES.map((language) => ({ language, scheme: 'file' })),
      hoverProvider,
    ),
  );

  // ── Sidebar TreeView ───────────────────────────────────────────────────
  const treeProvider = new AstraGraphTreeProvider(client);
  const treeView = vscode.window.createTreeView('astraGraphView', {
    treeDataProvider: treeProvider,
    showCollapseAll: true,
  });
  context.subscriptions.push(treeView);

  // ── Commands ───────────────────────────────────────────────────────────
  context.subscriptions.push(
    vscode.commands.registerCommand('astra.refreshGraphView', () => {
      treeProvider.refresh();
      codeLensProvider.refresh();
    }),
    vscode.commands.registerCommand('astra.showStatus', async () => {
      const reachable = await client.ping();
      if (reachable) {
        vscode.window.showInformationMessage('ASTra daemon/dashboard is reachable.');
      } else {
        const choice = await vscode.window.showWarningMessage(
          'ASTra daemon/dashboard is not reachable. Start it from a terminal in your project root.',
          'Copy start command',
        );
        if (choice === 'Copy start command') {
          await vscode.env.clipboard.writeText('astra daemon start && astra dashboard');
        }
      }
    }),
    vscode.commands.registerCommand('astra.revealSymbol', async (node: AstraNode) => {
      if (!node?.file) {
        return;
      }
      try {
        const doc = await vscode.workspace.openTextDocument(node.file);
        const editor = await vscode.window.showTextDocument(doc);
        const line = Math.max(0, (node.line_start ?? node.line ?? 1) - 1);
        const position = new vscode.Position(line, 0);
        editor.selection = new vscode.Selection(position, position);
        editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenter);
      } catch (err) {
        vscode.window.showWarningMessage(`ASTra: could not open ${node.file} (${String(err)})`);
      }
    }),
  );

  // Keep the tree/lenses reasonably fresh as the daemon/dashboard reindexes.
  // TODO(future contributor): subscribe to the daemon's WebSocket/graph_delta
  // broadcast (see AstraDaemon._broadcast in astra/daemon/core.py) instead of
  // polling, for instant updates on file save.
  const pollInterval = setInterval(() => {
    treeProvider.refresh();
  }, 15000);
  context.subscriptions.push({ dispose: () => clearInterval(pollInterval) });
}

export function deactivate(): void {
  // Nothing to clean up beyond what's registered in context.subscriptions.
}
