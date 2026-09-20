import type { ExtractRowV2, TableBlockV2 } from "../api/types";

export interface RowBlock {
  type: "table" | "para" | "tableV2";
  rows: string[][]; // para / legacy 表格用
  v2?: { tid: number; columnCount: number; columnWidthsTwips: number[]; rows: { cells: TableBlockV2["cells"] }[] };
}

/** Word 宽度单位换算（唯一入口）：1 inch = 1440 twips = 96px → px = twips / 15。 */
export function twipsToPx(twips: number): number {
  return Math.round((twips / 15) * 10) / 10;
}

export function isTableBlockV2(r: ExtractRowV2 | unknown): r is TableBlockV2 {
  return !!r && typeof r === "object" && !Array.isArray(r) && (r as TableBlockV2).type === "table";
}

/**
 * 将后端 rows 恢复成 Word 显示块。
 *
 * - 字符串数组（legacy 兼容）：1 列=段落、多列=表格行，连续多列合块。
 *   注意：这是旧 cells_json 数据的兼容规则，不是长期架构。
 * - TableBlockV2 对象：整表结构化渲染（colSpan/rowSpan/列宽显式 given），
 *   连续同 tid 的行归入同一张表。
 */
export function rowBlocks(rows: ExtractRowV2[]): RowBlock[] {
  const blocks: RowBlock[] = [];

  for (const rawRow of rows) {
    if (isTableBlockV2(rawRow)) {
      const last = blocks[blocks.length - 1];
      if (last?.type === "tableV2" && last.v2?.tid === rawRow.tid) {
        last.v2.rows.push({ cells: rawRow.cells });
      } else {
        blocks.push({
          type: "tableV2",
          rows: [],
          v2: {
            tid: rawRow.tid,
            columnCount: rawRow.columnCount,
            columnWidthsTwips: rawRow.columnWidthsTwips,
            rows: [{ cells: rawRow.cells }],
          },
        });
      }
      continue;
    }

    if (!Array.isArray(rawRow)) continue;

    const cells = rawRow.map((c) => String(c ?? "").replace(/\u00a0/g, " ").trim());

    // 全空行不参与显示
    if (!cells.some(Boolean)) continue;

    // 单列 = 普通正文段落。不要因为内容含「：」就转成表格。
    if (cells.length === 1) {
      blocks.push({ type: "para", rows: [[cells[0]]] });
      continue;
    }

    // 多列 = legacy 表格行；连续表格行合并为同一 table block。
    const last = blocks[blocks.length - 1];
    if (last?.type === "table") {
      last.rows.push(cells);
    } else {
      blocks.push({ type: "table", rows: [cells] });
    }
  }

  return blocks;
}
