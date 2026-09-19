import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'

/**
 * 极简代码编辑器（textarea + 等宽字体 + 行号）。
 *
 * 设计动机：Monaco Editor 体积约 3MB，会显著拖累首屏。HIA-70 前端只要求
 * 「能编辑 Python/JS/TS 源代码」，先用 textarea 满足功能，把 Monaco 升级
 * 留给未来 PR（仍可通过 `<CodeEditor>` 接口无缝替换）。
 *
 * 功能：
 *  - 行号侧栏（同步滚动）
 *  - Tab 插入 4 空格（编辑器体验基本项）
 *  - 支持 ref.current.insertAtCursor(text)
 *  - 支持 readOnly / 自适应高度 / 等宽字体
 */

interface Props {
  value: string
  onChange?: (next: string) => void
  language?: 'python' | 'javascript' | 'typescript' | 'json' | 'plain'
  readOnly?: boolean
  minRows?: number
  placeholder?: string
  className?: string
  'aria-label'?: string
}

export interface CodeEditorHandle {
  insertAtCursor: (text: string) => void
  focus: () => void
}

const LINE_NUMBER_GUTTER = 44 // px，留出足够宽度

export const CodeEditor = forwardRef<CodeEditorHandle, Props>(function CodeEditor(
  { value, onChange, readOnly = false, minRows = 12, placeholder, className = '', 'aria-label': ariaLabel },
  ref,
) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const gutterRef = useRef<HTMLDivElement | null>(null)

  useImperativeHandle(ref, () => ({
    insertAtCursor: (text: string) => {
      const ta = textareaRef.current
      if (!ta) return
      const start = ta.selectionStart ?? value.length
      const end = ta.selectionEnd ?? value.length
      const next = value.slice(0, start) + text + value.slice(end)
      onChange?.(next)
      // 下一帧把光标放到插入文本之后
      requestAnimationFrame(() => {
        ta.focus()
        const pos = start + text.length
        ta.setSelectionRange(pos, pos)
      })
    },
    focus: () => textareaRef.current?.focus(),
  }))

  // 同步行号栏滚动位置
  useEffect(() => {
    const ta = textareaRef.current
    const gutter = gutterRef.current
    if (!ta || !gutter) return
    const onScroll = () => {
      gutter.scrollTop = ta.scrollTop
    }
    ta.addEventListener('scroll', onScroll)
    return () => ta.removeEventListener('scroll', onScroll)
  }, [])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Tab' && !e.shiftKey && !e.altKey) {
      e.preventDefault()
      const ta = e.currentTarget
      const start = ta.selectionStart
      const end = ta.selectionEnd
      const insert = '    '
      const next = value.slice(0, start) + insert + value.slice(end)
      onChange?.(next)
      requestAnimationFrame(() => {
        ta.focus()
        ta.setSelectionRange(start + insert.length, start + insert.length)
      })
    }
  }

  const lineCount = Math.max(value.split('\n').length, minRows)

  return (
    <div
      className={`relative flex overflow-hidden rounded-md border border-slate-300 bg-white font-mono text-sm ${className}`}
    >
      <div
        ref={gutterRef}
        aria-hidden
        className="select-none overflow-hidden border-r border-slate-200 bg-slate-50 px-2 py-2 text-right text-xs leading-[1.5rem] text-slate-400"
        style={{ width: LINE_NUMBER_GUTTER, height: 'auto' }}
      >
        {Array.from({ length: lineCount }, (_, i) => (
          <div key={i}>{i + 1}</div>
        ))}
      </div>
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
        onKeyDown={handleKeyDown}
        readOnly={readOnly}
        placeholder={placeholder}
        aria-label={ariaLabel}
        spellCheck={false}
        rows={minRows}
        className="block w-full resize-none bg-white px-3 py-2 leading-[1.5rem] text-slate-900 outline-none focus:bg-sky-50/30"
        style={{ minHeight: `${minRows * 1.5}rem`, tabSize: 4 }}
      />
    </div>
  )
})
