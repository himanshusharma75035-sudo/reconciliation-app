import React from 'react'

// Lightweight, dependency-free Markdown renderer.
// Handles the subset our docs use: headings, paragraphs, bold/italic/inline-code,
// links, fenced code blocks, blockquotes, horizontal rules, ordered/unordered
// lists, and GFM tables. Fenced blocks tagged ```mermaid render as a labelled
// code block (real diagram rendering is a later enhancement).

// ── Inline formatting: `code`, **bold**, *italic*, [text](url) ────────────────
const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)]+\))/g

function renderInline(text, keyPrefix = 'i') {
  if (!text) return null
  const nodes = []
  let last = 0
  let m
  let n = 0
  INLINE_RE.lastIndex = 0
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const tok = m[0]
    const key = `${keyPrefix}-${n++}`
    if (tok.startsWith('`')) {
      nodes.push(<code key={key} className="px-1 py-0.5 rounded bg-gray-100 text-[0.85em] text-pink-700 font-mono">{tok.slice(1, -1)}</code>)
    } else if (tok.startsWith('**') || tok.startsWith('__')) {
      nodes.push(<strong key={key} className="font-semibold text-gray-800">{tok.slice(2, -2)}</strong>)
    } else if (tok.startsWith('[')) {
      const mm = tok.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
      nodes.push(<a key={key} href={mm[2]} target="_blank" rel="noreferrer" className="text-primary hover:underline">{mm[1]}</a>)
    } else { // *italic* / _italic_
      nodes.push(<em key={key}>{tok.slice(1, -1)}</em>)
    }
    last = m.index + tok.length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

// ── Block parsing ─────────────────────────────────────────────────────────────
function parseBlocks(md) {
  const lines = String(md || '').replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let i = 0

  const isTableSep = (s) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(s)

  while (i < lines.length) {
    let line = lines[i]

    // Fenced code block
    const fence = line.match(/^\s*```(\w*)\s*$/)
    if (fence) {
      const lang = fence[1] || ''
      const buf = []
      i++
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++ }
      i++ // skip closing fence
      blocks.push({ type: 'code', lang, content: buf.join('\n') })
      continue
    }

    // Blank line
    if (/^\s*$/.test(line)) { i++; continue }

    // Horizontal rule
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { blocks.push({ type: 'hr' }); i++; continue }

    // Heading
    const h = line.match(/^(#{1,6})\s+(.*)$/)
    if (h) { blocks.push({ type: 'heading', level: h[1].length, text: h[2] }); i++; continue }

    // Blockquote (consecutive > lines)
    if (/^\s*>\s?/.test(line)) {
      const buf = []
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, '')); i++ }
      blocks.push({ type: 'quote', text: buf.join(' ') })
      continue
    }

    // GFM table: a header line with |, followed by a separator line
    if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const splitRow = (s) => s.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim())
      const header = splitRow(line)
      i += 2
      const rows = []
      while (i < lines.length && lines[i].includes('|') && !/^\s*$/.test(lines[i])) {
        rows.push(splitRow(lines[i])); i++
      }
      blocks.push({ type: 'table', header, rows })
      continue
    }

    // Lists (consecutive list items)
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line)
      const items = []
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, '')); i++
      }
      blocks.push({ type: 'list', ordered, items })
      continue
    }

    // Paragraph (gather until blank / block boundary)
    const buf = [line]
    i++
    while (i < lines.length && !/^\s*$/.test(lines[i]) &&
           !/^\s*```/.test(lines[i]) && !/^(#{1,6})\s+/.test(lines[i]) &&
           !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) && !/^\s*>\s?/.test(lines[i])) {
      buf.push(lines[i]); i++
    }
    blocks.push({ type: 'para', text: buf.join(' ') })
  }
  return blocks
}

const H = {
  1: 'text-2xl font-bold text-gray-900 mt-6 mb-3 pb-1 border-b border-gray-200',
  2: 'text-xl font-bold text-gray-800 mt-6 mb-2',
  3: 'text-lg font-semibold text-gray-800 mt-4 mb-2',
  4: 'text-base font-semibold text-gray-700 mt-3 mb-1',
  5: 'text-sm font-semibold text-gray-700 mt-3 mb-1',
  6: 'text-sm font-semibold text-gray-500 mt-2 mb-1',
}

export default function Markdown({ content }) {
  const blocks = parseBlocks(content)
  return (
    <div className="text-sm text-gray-600 leading-relaxed">
      {blocks.map((b, idx) => {
        switch (b.type) {
          case 'heading': {
            const Tag = `h${b.level}`
            return <Tag key={idx} className={H[b.level]}>{renderInline(b.text, `h${idx}`)}</Tag>
          }
          case 'para':
            return <p key={idx} className="my-2">{renderInline(b.text, `p${idx}`)}</p>
          case 'hr':
            return <hr key={idx} className="my-4 border-gray-200" />
          case 'quote':
            return <blockquote key={idx} className="my-3 border-l-4 border-primary/30 bg-primary/5 pl-3 py-1.5 text-gray-600 italic">{renderInline(b.text, `q${idx}`)}</blockquote>
          case 'code':
            return (
              <div key={idx} className="my-3">
                {b.lang && <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1 font-mono">{b.lang}</div>}
                <pre className="bg-gray-900 text-gray-100 rounded-lg p-3 overflow-x-auto text-[12.5px] leading-snug"><code className="font-mono">{b.content}</code></pre>
              </div>
            )
          case 'list': {
            const Tag = b.ordered ? 'ol' : 'ul'
            return (
              <Tag key={idx} className={`my-2 ml-5 space-y-1 ${b.ordered ? 'list-decimal' : 'list-disc'}`}>
                {b.items.map((it, j) => <li key={j}>{renderInline(it, `l${idx}-${j}`)}</li>)}
              </Tag>
            )
          }
          case 'table':
            return (
              <div key={idx} className="my-3 overflow-x-auto">
                <table className="w-full text-xs border border-gray-200 rounded-lg overflow-hidden">
                  <thead className="bg-gray-50">
                    <tr>{b.header.map((h, j) => <th key={j} className="text-left px-2.5 py-1.5 font-semibold text-gray-600 border-b border-gray-200">{renderInline(h, `th${idx}-${j}`)}</th>)}</tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j} className="border-b border-gray-100 last:border-0">
                        {r.map((c, k) => <td key={k} className="px-2.5 py-1.5 text-gray-600 align-top">{renderInline(c, `td${idx}-${j}-${k}`)}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          default:
            return null
        }
      })}
    </div>
  )
}
