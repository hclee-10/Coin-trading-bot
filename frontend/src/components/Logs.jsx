import { useEffect, useRef, useState } from 'react'

export default function Logs({ entries }) {
  const boxRef = useRef(null)
  const [follow, setFollow] = useState(true)

  // 사용자가 위로 스크롤해 과거 로그를 읽는 중이면 자동 스크롤을 멈춘다.
  function onScroll() {
    const box = boxRef.current
    if (!box) return
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40
    setFollow(atBottom)
  }

  useEffect(() => {
    if (follow && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight
    }
  }, [entries, follow])

  return (
    <section className="panel">
      <h2>
        로그
        <span className="spacer" />
        {!follow && (
          <button
            className="ghost"
            style={{ padding: '2px 10px', fontSize: 12 }}
            onClick={() => setFollow(true)}
          >
            최신으로
          </button>
        )}
      </h2>
      <div className="logs" ref={boxRef} onScroll={onScroll}>
        {entries.length === 0 ? (
          <div className="empty">아직 로그가 없습니다</div>
        ) : (
          entries.map((e) => (
            <div className="log-line" key={e.seq}>
              <span className="log-time">{e.at.slice(11, 19)}</span>
              <span className={`log-level lv-${e.level}`}>{e.level}</span>
              <span>{e.message}</span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}
