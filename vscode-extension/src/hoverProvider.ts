/**
 * hoverProvider.ts
 *
 * Richer markdown hover for a symbol: full caller list, an importance
 * indicator (proxy for PageRank — see astraClient.ts TODO), and a semantic
 * drift warning if the backend has one (currently always absent — no API
 * route exists yet, see astraClient.getDriftWarning()).
 */

import * as vscode from 'vscode';
import { AstraClient, AstraNode } from './astraClient';

function formatNodeList(nodes: AstraNode[], limit = 10): string {
  if (nodes.length === 0) {
    return '_none_';
  }
  const shown = nodes.slice(0, limit);
  const lines = shown.map((n) => `- \`${n.name}\` — ${n.file}${n.line_start ? `:${n.line_start}` : ''}`);
  if (nodes.length > limit) {
    lines.push(`- _...and ${nodes.length - limit} more_`);
  }
  return lines.join('\n');
}

export class AstraHoverProvider implements vscode.HoverProvider {
  constructor(private readonly client: AstraClient) {}

  async provideHover(
    document: vscode.TextDocument,
    position: vscode.Position,
    _token: vscode.CancellationToken,
  ): Promise<vscode.Hover | undefined> {
    const wordRange = document.getWordRangeAtPosition(position, /[A-Za-z_$][A-Za-z0-9_$]*/);
    if (!wordRange) {
      return undefined;
    }
    const word = document.getText(wordRange);

    const reachable = await this.client.ping();
    if (!reachable) {
      return undefined; // daemon/dashboard down — no hover, no error noise
    }

    const nodes = await this.client.getSymbolsForFile(document.uri.fsPath);
    const node = nodes.find((n) => n.name === word);
    if (!node) {
      return undefined;
    }

    const detail = await this.client.getNodeDetail(node.id);
    if (!detail) {
      return undefined;
    }

    const importance = await this.client.getImportance(node.id);
    const drift = await this.client.getDriftWarning(node.id);

    const md = new vscode.MarkdownString();
    md.isTrusted = true;
    md.supportThemeIcons = true;

    md.appendMarkdown(`### \`${node.name}\` (${node.type})\n\n`);
    if (node.signature) {
      md.appendCodeblock(node.signature, languageIdForFile(node.file));
    }
    if (node.docstring) {
      md.appendMarkdown(`${node.docstring}\n\n`);
    }

    if (importance) {
      const icon = importance.tier === 'high' ? '$(flame)' : importance.tier === 'medium' ? '$(star)' : '$(circle-outline)';
      md.appendMarkdown(`${icon} **Importance: ${importance.tier}** _(proxy: ${importance.callerCount} callers — real PageRank not yet exposed by the ASTra API, see astraClient.ts TODO)_\n\n`);
    }

    md.appendMarkdown(`**Callers (${detail.callers.length}):**\n\n${formatNodeList(detail.callers)}\n\n`);
    md.appendMarkdown(`**Callees (${detail.callees.length}):**\n\n${formatNodeList(detail.callees)}\n\n`);

    if (drift) {
      md.appendMarkdown(
        `---\n\n$(warning) **Semantic drift detected** (score ${drift.drift_score.toFixed(2)})\n\n` +
          `${drift.explanation}\n\n` +
          `Declared intent: _${drift.declared_intent}_\n\n` +
          `Actual callees: ${drift.actual_callees.join(', ') || '_none_'}`,
      );
    }

    return new vscode.Hover(md, wordRange);
  }
}

function languageIdForFile(file: string): string {
  const ext = file.split('.').pop()?.toLowerCase();
  switch (ext) {
    case 'py':
      return 'python';
    case 'ts':
      return 'typescript';
    case 'tsx':
      return 'typescriptreact';
    case 'js':
      return 'javascript';
    case 'jsx':
      return 'javascriptreact';
    case 'go':
      return 'go';
    case 'rs':
      return 'rust';
    case 'java':
      return 'java';
    default:
      return '';
  }
}
