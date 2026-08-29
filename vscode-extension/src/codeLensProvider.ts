/**
 * codeLensProvider.ts
 *
 * Shows an inline CodeLens above function/class definitions with a quick
 * summary: "N callers · M callees · importance: <tier>".
 *
 * Symbol detection here is intentionally simple (regex-based per-language
 * "def"/"function"/"class" scanning) rather than a full AST parse — the
 * source of truth for symbol boundaries is ASTra's own indexer; this
 * extension only needs to find *approximate* line anchors to place lenses
 * on, then match them up against ASTra's `line_start` for the file.
 */

import * as vscode from 'vscode';
import { AstraClient, AstraNode } from './astraClient';

interface LineAnchor {
  line: number;
  name: string;
}

const DEF_PATTERNS: RegExp[] = [
  /^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/, // python
  /^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)/, // python / java / etc
  /^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/, // js/ts
  /^\s*(?:public|private|protected|static|\s)*[\w<>\[\]]+\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\([^;{]*\)\s*\{/, // java-ish
  /^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(/, // go
  /^\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/, // rust
];

function findDefLines(document: vscode.TextDocument): LineAnchor[] {
  const anchors: LineAnchor[] = [];
  for (let i = 0; i < document.lineCount; i++) {
    const text = document.lineAt(i).text;
    for (const pattern of DEF_PATTERNS) {
      const match = pattern.exec(text);
      if (match) {
        anchors.push({ line: i, name: match[1] });
        break;
      }
    }
  }
  return anchors;
}

/** Match a source line anchor to the closest ASTra node by name + nearby line number. */
function matchNode(anchor: LineAnchor, nodes: AstraNode[]): AstraNode | undefined {
  const sameName = nodes.filter((n) => n.name === anchor.name);
  if (sameName.length === 0) {
    return undefined;
  }
  if (sameName.length === 1) {
    return sameName[0];
  }
  // Multiple overloads/methods with the same name — pick the closest by line.
  let best: AstraNode | undefined;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const n of sameName) {
    const nodeLine = (n.line_start ?? n.line ?? 1) - 1; // ASTra lines are 1-based
    const dist = Math.abs(nodeLine - anchor.line);
    if (dist < bestDist) {
      bestDist = dist;
      best = n;
    }
  }
  return best;
}

export class AstraCodeLensProvider implements vscode.CodeLensProvider {
  private readonly onDidChangeCodeLensesEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.onDidChangeCodeLensesEmitter.event;

  constructor(private readonly client: AstraClient) {}

  refresh(): void {
    this.onDidChangeCodeLensesEmitter.fire();
  }

  async provideCodeLenses(document: vscode.TextDocument): Promise<vscode.CodeLens[]> {
    if (!vscode.workspace.getConfiguration('astra').get<boolean>('enableCodeLens', true)) {
      return [];
    }

    const reachable = await this.client.ping();
    if (!reachable) {
      // Daemon/dashboard not running — degrade gracefully, no lenses, no throw.
      return [];
    }

    const nodes = await this.client.getSymbolsForFile(document.uri.fsPath);
    if (nodes.length === 0) {
      return [];
    }

    const anchors = findDefLines(document);
    const lenses: vscode.CodeLens[] = [];

    for (const anchor of anchors) {
      const node = matchNode(anchor, nodes);
      if (!node) {
        continue;
      }
      const range = document.lineAt(anchor.line).range;
      // Defer the actual counts to resolveCodeLens for snappier initial render.
      const lens = new vscode.CodeLens(range);
      (lens as any).astraNodeId = node.id;
      lenses.push(lens);
    }
    return lenses;
  }

  async resolveCodeLens(lens: vscode.CodeLens): Promise<vscode.CodeLens> {
    const nodeId: string | undefined = (lens as any).astraNodeId;
    if (!nodeId) {
      lens.command = { title: 'ASTra: no data', command: '' };
      return lens;
    }

    const importance = await this.client.getImportance(nodeId);
    if (!importance) {
      lens.command = { title: 'ASTra: unavailable', command: '' };
      return lens;
    }

    const label =
      `${importance.callerCount} caller${importance.callerCount === 1 ? '' : 's'} · ` +
      `${importance.calleeCount} callee${importance.calleeCount === 1 ? '' : 's'} · ` +
      `importance: ${importance.tier}`;

    lens.command = {
      title: label,
      command: 'astra.showStatus',
    };
    return lens;
  }
}
