import { useState } from 'react'

/**
 * 把任意 JSON 值渲染成可读视图：默认折叠 + 行号 + 切换展开。
 *
 * - 字符串：原样显示
 * - 数字 / 布尔 / null：彩色 token
 * - 对象 / 数组：折叠为 `{N items}` / `[N items]`，点击展开
 */

type JsonValue =
  | string
  | number
  | boolean
  | null
  | undefined
  | JsonValue[]
  | { [key: string]: JsonValue }

interface Props {
  value: JsonValue | unknown
  className?: string
  defaultExpanded?: boolean
  emptyText?: string
}

function isObject(v: unknown): v is { [key: string]: JsonValue } {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

export function JsonView({ value, className = '', defaultExpanded = false, emptyText = '（空）' }: Props) {
  if (value === undefined || value === null) {
    return <span className="text-xs text-slate-400">{emptyText}</span>
  }
  return (
    <pre
      className={`overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-relaxed ${className}`}
    >
      <Node value={value as JsonValue} depth={0} defaultExpanded={defaultExpanded} />
    </pre>
  )
}

function Node({
  value,
  depth,
  defaultExpanded,
  keyName,
}: {
  value: JsonValue
  depth: number
  defaultExpanded: boolean
  keyName?: string
}) {
  const [expanded, setExpanded] = useState(defaultExpanded || depth < 1)

  if (value === null) return <Token color="text-slate-400">null</Token>
  if (value === undefined) return <Token color="text-slate-400">undefined</Token>
  if (typeof value === 'string')
    return (
      <>
        {keyName !== undefined && <KeyLabel>{keyName}</KeyLabel>}
        <Token color="text-emerald-700">"{value}"</Token>
      </>
    )
  if (typeof value === 'number')
    return (
      <>
        {keyName !== undefined && <KeyLabel>{keyName}</KeyLabel>}
        <Token color="text-sky-700">{String(value)}</Token>
      </>
    )
  if (typeof value === 'boolean')
    return (
      <>
        {keyName !== undefined && <KeyLabel>{keyName}</KeyLabel>}
        <Token color="text-amber-700">{String(value)}</Token>
      </>
    )

  if (Array.isArray(value)) {
    if (value.length === 0)
      return (
        <>
          {keyName !== undefined && <KeyLabel>{keyName}</KeyLabel>}
          <Token color="text-slate-500">[]</Token>
        </>
      )
    return (
      <>
        {keyName !== undefined && <KeyLabel>{keyName}</KeyLabel>}
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mr-1 rounded text-slate-500 hover:text-slate-900"
        >
          {expanded ? '▾' : '▸'}
        </button>
        <span className="text-slate-500">{expanded ? '[' : `[${value.length} items]`}</span>
        {expanded && (
          <div className="ml-4 border-l border-slate-200 pl-3">
            {value.map((v, i) => (
              <div key={i}>
                <Node value={v} depth={depth + 1} defaultExpanded={defaultExpanded} />
              </div>
            ))}
            <span className="text-slate-500">]</span>
          </div>
        )}
      </>
    )
  }

  if (isObject(value)) {
    const keys = Object.keys(value)
    if (keys.length === 0)
      return (
        <>
          {keyName !== undefined && <KeyLabel>{keyName}</KeyLabel>}
          <Token color="text-slate-500">{'{}'}</Token>
        </>
      )
    return (
      <>
        {keyName !== undefined && <KeyLabel>{keyName}</KeyLabel>}
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mr-1 rounded text-slate-500 hover:text-slate-900"
        >
          {expanded ? '▾' : '▸'}
        </button>
        <span className="text-slate-500">{expanded ? '{' : `{${keys.length} keys}`}</span>
        {expanded && (
          <div className="ml-4 border-l border-slate-200 pl-3">
            {keys.map((k) => (
              <div key={k}>
                <Node value={value[k]} depth={depth + 1} defaultExpanded={defaultExpanded} keyName={k} />
              </div>
            ))}
            <span className="text-slate-500">{'}'}</span>
          </div>
        )}
      </>
    )
  }

  return <span>{String(value)}</span>
}

function Token({ children, color }: { children: React.ReactNode; color: string }) {
  return <span className={color}>{children}</span>
}

function KeyLabel({ children }: { children: React.ReactNode }) {
  return <span className="text-slate-600">"{children}": </span>
}
