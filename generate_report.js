// generate_report.js — DOCX report writer for the adversarial debate tool.
// Usage: node generate_report.js output/debate_data.json
//
// Reads debate_data.json, emits output/debate_report.docx.
// Strict rules (see CLAUDE spec): WidthType.DXA only, ShadingType.CLEAR only,
// PageBreak inside Paragraph, no \n inside TextRun, no unicode bullets.

const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  PageBreak, LevelFormat, PageOrientation, convertInchesToTwip,
} = require("docx");

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const A4_WIDTH_DXA = 11906;
const A4_HEIGHT_DXA = 16838;
const MARGIN_DXA = 1440;          // 1 inch
const CONTENT_WIDTH_DXA = 9026;   // A4 minus 1" margins (both sides)

const CELL_MARGINS = { top: 80, bottom: 80, left: 120, right: 120 };

const BAND_COLOURS = {
  GREEN:    { bg: "E2EFDA", fg: "375623" },
  AMBER:    { bg: "FFF2CC", fg: "7D5A00" },
  RED:      { bg: "FCE4D6", fg: "843C0C" },
  CRITICAL: { bg: "F4CCCC", fg: "660000" },
};

const HEADER_GREY = "D5E8F0";
const CONCESSION_HEADER = "FCE4D6";
const REBUTTAL_BG = "F3F3F3";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function tr(text, opts = {}) {
  // TextRun must never contain \n.
  return new TextRun({
    text: String(text == null ? "" : text).replace(/\r?\n/g, " "),
    font: "Arial",
    size: opts.size ?? 24,            // half-points → 24 = 12pt
    bold: !!opts.bold,
    italics: !!opts.italics,
    color: opts.color || "000000",
  });
}

function para(runs, opts = {}) {
  return new Paragraph({
    children: Array.isArray(runs) ? runs : [runs],
    alignment: opts.alignment,
    spacing: opts.spacing,
    indent: opts.indent,
    pageBreakBefore: !!opts.pageBreakBefore,
  });
}

function heading(text, level) {
  return new Paragraph({
    heading: level,
    children: [tr(text, { bold: true,
      size: level === HeadingLevel.HEADING_1 ? 36
           : level === HeadingLevel.HEADING_2 ? 28
           : 24,
      color: level === HeadingLevel.HEADING_1 ? "1F3864"
           : level === HeadingLevel.HEADING_2 ? "2E5096"
           : "404040" })],
  });
}

function shadedCell({ text, widthDxa, bg, fg, bold = false, alignment, runs }) {
  return new TableCell({
    width: { size: widthDxa, type: WidthType.DXA },
    margins: CELL_MARGINS,
    shading: bg ? { type: ShadingType.CLEAR, color: "auto", fill: bg } : undefined,
    children: [
      new Paragraph({
        alignment,
        children: runs || [tr(text, { bold, color: fg || "000000" })],
      }),
    ],
  });
}

function plainCell(text, widthDxa, opts = {}) {
  return shadedCell({
    text, widthDxa, bg: opts.bg, fg: opts.fg,
    bold: opts.bold, alignment: opts.alignment, runs: opts.runs,
  });
}

function table(rows, columnWidths) {
  return new Table({
    width: { size: CONTENT_WIDTH_DXA, type: WidthType.DXA },
    columnWidths,
    rows,
  });
}

// ---------------------------------------------------------------------------
// Section builders
// ---------------------------------------------------------------------------
function buildCoverPage(data) {
  const m = data.metadata;
  const band = data.resilience_score.band;
  const colours = BAND_COLOURS[band] || BAND_COLOURS.AMBER;

  const scoreTable = table(
    [new TableRow({
      children: [shadedCell({
        widthDxa: CONTENT_WIDTH_DXA,
        bg: colours.bg, fg: colours.fg,
        alignment: AlignmentType.CENTER,
        runs: [tr(
          `Resilience Score: ${data.resilience_score.total}/100 — ${band}`,
          { bold: true, size: 36, color: colours.fg },
        )],
      })],
    })],
    [CONTENT_WIDTH_DXA],
  );

  const personaRows = ["agent_1","agent_2","agent_3","agent_4"].map((aid) => {
    const p = data.personas[aid];
    return new TableRow({
      children: [
        plainCell(displayName(aid), 3500),
        plainCell(`${p.name} (${p.type})`, CONTENT_WIDTH_DXA - 3500),
      ],
    });
  });
  const personasTable = table(
    [new TableRow({
      children: [
        plainCell("Agent", 3500, { bg: HEADER_GREY, bold: true }),
        plainCell("Persona Name & Type", CONTENT_WIDTH_DXA - 3500, { bg: HEADER_GREY, bold: true }),
      ],
    }), ...personaRows],
    [3500, CONTENT_WIDTH_DXA - 3500],
  );

  return [
    para([tr("Adversarial Debate Report", { bold: true, size: 48, color: "1F3864" })],
      { alignment: AlignmentType.CENTER, spacing: { before: 400, after: 200 } }),
    para([tr(m.proposal_title, { bold: true, size: 32, color: "2E5096" })],
      { alignment: AlignmentType.CENTER, spacing: { after: 200 } }),
    para([tr(`Date generated: ${m.debate_date}`, { italics: true, size: 22, color: "666666" })],
      { alignment: AlignmentType.CENTER }),
    para([tr(`Input source: ${m.input_source.toUpperCase()}`, { bold: true, size: 22 })],
      { alignment: AlignmentType.CENTER, spacing: { after: 300 } }),
    scoreTable,
    para([tr("", {})], { spacing: { after: 200 } }),
    personasTable,
    para([new PageBreak()]),
  ];
}

function buildToc() {
  return [
    heading("Contents", HeadingLevel.HEADING_1),
    para([tr("Cover Page", {})]),
    para([tr("Executive Summary", {})]),
    para([tr("Debate Transcript", {})]),
    para([tr("  Round 1 — Diagnostic", {})]),
    para([tr("  Round 2 — Direct Rebuttal", {})]),
    para([tr("  Round 3 — Final Verdict", {})]),
    para([tr("Analysis", {})]),
    para([tr("  Unrefuted Weaknesses", {})]),
    para([tr("  Concessions Log", {})]),
    para([tr("  Recommended Amendments", {})]),
    para([tr("Appendix", {})]),
    para([new PageBreak()]),
  ];
}

function buildExecSummary(data) {
  const out = [heading("Executive Summary", HeadingLevel.HEADING_1)];
  out.push(para([tr(data.proposal_summary || "")]));

  const b = data.resilience_score.breakdown;
  const summaryRows = [
    new TableRow({
      children: [
        plainCell("Factor", 4500, { bg: HEADER_GREY, bold: true }),
        plainCell("Raw Score", 2000, { bg: HEADER_GREY, bold: true }),
        plainCell("Deduction", CONTENT_WIDTH_DXA - 6500, { bg: HEADER_GREY, bold: true }),
      ],
    }),
    new TableRow({ children: [
      plainCell("Unrefuted Weaknesses", 4500),
      plainCell(String(b.unrefuted_weaknesses.count), 2000),
      plainCell(`-${b.unrefuted_weaknesses.deduction}`, CONTENT_WIDTH_DXA - 6500),
    ]}),
    new TableRow({ children: [
      plainCell("CRITICAL Risks", 4500),
      plainCell(String(b.critical_risks.count), 2000),
      plainCell(`-${b.critical_risks.deduction}`, CONTENT_WIDTH_DXA - 6500),
    ]}),
    new TableRow({ children: [
      plainCell("Concessions", 4500),
      plainCell(String(b.concessions.count), 2000),
      plainCell(`-${b.concessions.deduction}`, CONTENT_WIDTH_DXA - 6500),
    ]}),
    new TableRow({ children: [
      plainCell("GO/NO-GO Split", 4500),
      plainCell(`${b.go_nogo_split.go_count} GO`, 2000),
      plainCell(`+${b.go_nogo_split.score}`, CONTENT_WIDTH_DXA - 6500),
    ]}),
  ];
  out.push(table(summaryRows, [4500, 2000, CONTENT_WIDTH_DXA - 6500]));

  out.push(para([tr("", {})], { spacing: { before: 200 } }));

  // GO/NO-GO summary
  const personaLookup = {
    "Advocate": data.personas.agent_1.name,
    "Devil's Advocate": data.personas.agent_2.name,
    "Risk & Legal": data.personas.agent_3.name,
    "Adaptive Stakeholder": data.personas.agent_4.name,
  };
  const r3 = data.round_3 || [];
  const headerRow = new TableRow({ children: [
    plainCell("Agent", 2200, { bg: HEADER_GREY, bold: true }),
    plainCell("Persona", 2500, { bg: HEADER_GREY, bold: true }),
    plainCell("Verdict", 1500, { bg: HEADER_GREY, bold: true }),
    plainCell("Critical Condition", CONTENT_WIDTH_DXA - 6200, { bg: HEADER_GREY, bold: true }),
  ]});
  const dataRows = r3.map((r) => {
    const verdictBg = r.verdict === "GO" ? "E2EFDA" : "FCE4D6";
    return new TableRow({ children: [
      plainCell(r.agent || "", 2200),
      plainCell(personaLookup[r.agent] || "", 2500),
      plainCell(r.verdict || "", 1500, { bg: verdictBg, bold: true }),
      plainCell(r.critical_condition || "", CONTENT_WIDTH_DXA - 6200),
    ]});
  });
  out.push(table([headerRow, ...dataRows],
    [2200, 2500, 1500, CONTENT_WIDTH_DXA - 6200]));

  out.push(para([new PageBreak()]));
  return out;
}

function buildTranscript(data) {
  const out = [heading("Debate Transcript", HeadingLevel.HEADING_1)];

  // Round 1
  out.push(heading("Round 1 — Diagnostic", HeadingLevel.HEADING_2));
  (data.round_1 || []).forEach((entry, idx) => {
    out.push(heading(`${entry.agent} (${entry.persona_applied || ""})`, HeadingLevel.HEADING_3));
    (entry.items || []).forEach((it, i) => {
      const runs = [
        tr(`${it.id}: `, { bold: true }),
        tr(it.description || ""),
      ];
      if (it.severity) runs.push(tr(` [${it.severity}]`, { bold: true, color: "843C0C" }));
      out.push(new Paragraph({
        numbering: { reference: "decimal-list", level: 0 },
        children: runs,
      }));
    });
    if (typeof entry.confidence === "number") {
      out.push(para([tr(`Confidence: ${entry.confidence}`, { italics: true, size: 20, color: "666666" })]));
    }
  });

  // Round 2
  out.push(heading("Round 2 — Direct Rebuttal", HeadingLevel.HEADING_2));
  (data.round_2 || []).forEach((entry) => {
    out.push(heading(`${entry.agent}`, HeadingLevel.HEADING_3));
    const cx = entry.cross_examination || {};
    const cell = new TableCell({
      width: { size: CONTENT_WIDTH_DXA, type: WidthType.DXA },
      margins: CELL_MARGINS,
      shading: { type: ShadingType.CLEAR, color: "auto", fill: REBUTTAL_BG },
      children: [
        new Paragraph({ children: [
          tr(`${cx.action || ""} `, { bold: true,
            color: cx.action === "CONCEDE" ? "843C0C" : "1F3864" }),
          tr(`[${cx.target_label || ""}]`, { bold: true }),
        ]}),
        new Paragraph({ children: [tr(cx.justification || "")] }),
      ],
    });
    out.push(new Table({
      width: { size: CONTENT_WIDTH_DXA, type: WidthType.DXA },
      columnWidths: [CONTENT_WIDTH_DXA],
      rows: [new TableRow({ children: [cell] })],
    }));
    if (entry.position_update) {
      out.push(para([tr("Position update: ", { bold: true }), tr(entry.position_update)]));
    }
  });

  // Round 3
  out.push(heading("Round 3 — Final Verdict", HeadingLevel.HEADING_2));
  (data.round_3 || []).forEach((entry) => {
    out.push(heading(`${entry.agent}`, HeadingLevel.HEADING_3));
    const verdictColor = entry.verdict === "GO" ? "375623" : "843C0C";
    out.push(para([tr(entry.verdict || "", { bold: true, color: verdictColor, size: 28 })]));
    out.push(new Paragraph({
      indent: { left: convertInchesToTwip(0.5) },
      children: [tr("Critical condition: ", { bold: true }),
                 tr(entry.critical_condition || "")],
    }));
  });

  out.push(para([new PageBreak()]));
  return out;
}

function buildAnalysis(data) {
  const out = [heading("Analysis", HeadingLevel.HEADING_1)];

  // Unrefuted Weaknesses
  out.push(heading("Unrefuted Weaknesses", HeadingLevel.HEADING_2));
  const uw = (data.synthesis.unrefuted_weaknesses || [])
    .slice().sort((a, b) => (b.cited_by || 0) - (a.cited_by || 0));
  if (uw.length === 0) {
    out.push(para([tr("None.", { italics: true })]));
  } else {
    const header = new TableRow({ children: [
      plainCell("Label", 1800, { bg: HEADER_GREY, bold: true }),
      plainCell("Description", CONTENT_WIDTH_DXA - 3000, { bg: HEADER_GREY, bold: true }),
      plainCell("Cited By", 1200, { bg: HEADER_GREY, bold: true }),
    ]});
    const rows = uw.map((w) => new TableRow({ children: [
      plainCell(w.label || "", 1800),
      plainCell(w.description || "", CONTENT_WIDTH_DXA - 3000),
      plainCell(String(w.cited_by || 0), 1200),
    ]}));
    out.push(table([header, ...rows], [1800, CONTENT_WIDTH_DXA - 3000, 1200]));
  }

  // Concessions Log
  out.push(heading("Concessions Log", HeadingLevel.HEADING_2));
  const cl = data.synthesis.concessions_log || [];
  if (cl.length === 0) {
    out.push(para([tr("None.", { italics: true })]));
  } else {
    const header = new TableRow({ children: [
      plainCell("Agent", 2200, { bg: CONCESSION_HEADER, bold: true }),
      plainCell("Target Label", 1800, { bg: CONCESSION_HEADER, bold: true }),
      plainCell("Justification", CONTENT_WIDTH_DXA - 4000, { bg: CONCESSION_HEADER, bold: true }),
    ]});
    const rows = cl.map((c) => new TableRow({ children: [
      plainCell(c.agent || "", 2200),
      plainCell(c.label || "", 1800),
      plainCell(c.justification || "", CONTENT_WIDTH_DXA - 4000),
    ]}));
    out.push(table([header, ...rows], [2200, 1800, CONTENT_WIDTH_DXA - 4000]));
  }

  // Recommended Amendments
  out.push(heading("Recommended Amendments", HeadingLevel.HEADING_2));
  (data.synthesis.recommended_amendments || []).forEach((rec) => {
    out.push(new Paragraph({
      numbering: { reference: "decimal-list", level: 0 },
      children: [tr(rec)],
    }));
  });

  out.push(para([new PageBreak()]));
  return out;
}

function buildAppendix(data) {
  const out = [heading("Appendix", HeadingLevel.HEADING_1)];

  out.push(heading("Persona Configuration", HeadingLevel.HEADING_2));
  const personaHeader = new TableRow({ children: [
    plainCell("Agent", 2500, { bg: HEADER_GREY, bold: true }),
    plainCell("Persona Name", CONTENT_WIDTH_DXA - 4000, { bg: HEADER_GREY, bold: true }),
    plainCell("Type", 1500, { bg: HEADER_GREY, bold: true }),
  ]});
  const personaRows = ["agent_1","agent_2","agent_3","agent_4"].map((aid) => {
    const p = data.personas[aid];
    return new TableRow({ children: [
      plainCell(displayName(aid), 2500),
      plainCell(p.name, CONTENT_WIDTH_DXA - 4000),
      plainCell(p.type, 1500),
    ]});
  });
  out.push(table([personaHeader, ...personaRows], [2500, CONTENT_WIDTH_DXA - 4000, 1500]));

  out.push(heading("Resilience Score Methodology", HeadingLevel.HEADING_2));
  const b = data.resilience_score.breakdown;
  const methodHeader = new TableRow({ children: [
    plainCell("Factor", 3000, { bg: HEADER_GREY, bold: true }),
    plainCell("Weight", 1200, { bg: HEADER_GREY, bold: true }),
    plainCell("Scoring Logic", CONTENT_WIDTH_DXA - 5400, { bg: HEADER_GREY, bold: true }),
    plainCell("Points Applied", 1200, { bg: HEADER_GREY, bold: true }),
  ]});
  const methodRows = [
    ["Unrefuted Weaknesses", "40%", "-5 pts per unrefuted weakness", `-${b.unrefuted_weaknesses.deduction}`],
    ["CRITICAL Risks", "25%", "-20 pts per CRITICAL risk", `-${b.critical_risks.deduction}`],
    ["Concessions", "20%", "-4 pts per concession", `-${b.concessions.deduction}`],
    ["GO/NO-GO Split", "15%", "4 GO=+15, 3 GO=+10, 2 GO=+5, <2 GO=+0", `+${b.go_nogo_split.score}`],
  ].map((r) => new TableRow({ children: [
    plainCell(r[0], 3000),
    plainCell(r[1], 1200),
    plainCell(r[2], CONTENT_WIDTH_DXA - 5400),
    plainCell(r[3], 1200),
  ]}));
  out.push(table([methodHeader, ...methodRows], [3000, 1200, CONTENT_WIDTH_DXA - 5400, 1200]));

  return out;
}

function displayName(aid) {
  return {
    agent_1: "Advocate",
    agent_2: "Devil's Advocate",
    agent_3: "Risk & Legal",
    agent_4: "Adaptive Stakeholder",
  }[aid] || aid;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
function main() {
  const dataPath = process.argv[2] || "output/debate_data.json";
  const data = JSON.parse(fs.readFileSync(dataPath, "utf-8"));

  const children = [
    ...buildCoverPage(data),
    ...buildToc(),
    ...buildExecSummary(data),
    ...buildTranscript(data),
    ...buildAnalysis(data),
    ...buildAppendix(data),
  ];

  const doc = new Document({
    creator: "Adversarial Debate Tool",
    title: data.metadata.proposal_title,
    styles: {
      default: {
        document: { run: { font: "Arial", size: 24 } },
      },
      paragraphStyles: [
        {
          id: "Heading1",
          name: "Heading 1",
          basedOn: "Normal",
          next: "Normal",
          quickFormat: true,
          run: { font: "Arial", size: 36, bold: true, color: "1F3864" },
          paragraph: {
            spacing: { before: 240, after: 120 },
            outlineLevel: 0,
          },
        },
        {
          id: "Heading2",
          name: "Heading 2",
          basedOn: "Normal",
          next: "Normal",
          quickFormat: true,
          run: { font: "Arial", size: 28, bold: true, color: "2E5096" },
          paragraph: {
            spacing: { before: 180, after: 90 },
            outlineLevel: 1,
          },
        },
        {
          id: "Heading3",
          name: "Heading 3",
          basedOn: "Normal",
          next: "Normal",
          quickFormat: true,
          run: { font: "Arial", size: 24, bold: true, color: "404040" },
          paragraph: {
            spacing: { before: 120, after: 60 },
            outlineLevel: 2,
          },
        },
      ],
    },
    numbering: {
      config: [
        {
          reference: "decimal-list",
          levels: [
            {
              level: 0,
              format: LevelFormat.DECIMAL,
              text: "%1.",
              alignment: AlignmentType.START,
              style: { paragraph: { indent: { left: 720, hanging: 360 } } },
            },
          ],
        },
      ],
    },
    sections: [{
      properties: {
        page: {
          size: { width: A4_WIDTH_DXA, height: A4_HEIGHT_DXA, orientation: PageOrientation.PORTRAIT },
          margin: { top: MARGIN_DXA, right: MARGIN_DXA, bottom: MARGIN_DXA, left: MARGIN_DXA },
        },
      },
      children,
    }],
  });

  const outPath = path.join("output", "debate_report.docx");
  Packer.toBuffer(doc).then((buf) => {
    fs.mkdirSync("output", { recursive: true });
    fs.writeFileSync(outPath, buf);
    console.log(`[INFO] Wrote ${outPath}`);
  }).catch((err) => {
    console.error("[ERROR] DOCX generation failed:", err);
    process.exit(1);
  });
}

main();
