export interface RowBlock {
  type: "table" | "para";
  rows: string[][];
}

/**
 * 将后端 rows 恢复成 Word 显示块（兼容规则：1 列=正文段落、多列=表格行）。
 *
 * 注意：这是当前 cells_json 数据结构下的兼容修复规则，不是长期架构——
 * 后续 Block Schema V2 落地后，段落/表格将由后端显式类型标记，不再按列数推断。
 */
export function rowBlocks(rows: string[][]): RowBlock[] {
  const blocks: RowBlock[] = [];

  for (const rawRow of rows) {
    if (!Array.isArray(rawRow)) continue;

    const cells = rawRow.map((c) => String(c ?? "").replace(/\u00a0/g, " ").trim());

    // 全空行不参与显示
    if (!cells.some(Boolean)) continue;

    // 单列 = 普通正文段落。不要因为内容含「：」就转成表格。
    if (cells.length === 1) {
      blocks.push({ type: "para", rows: [[cells[0]]] });
      continue;
    }

    // 多列 = Word 表格行；连续表格行合并为同一 table block。
    const last = blocks[blocks.length - 1];
    if (last?.type === "table") {
      last.rows.push(cells);
    } else {
      blocks.push({ type: "table", rows: [cells] });
    }
  }

  return blocks;
}
