// 기록이 재배포를 넘어 남지 않을 때 띄우는 경고.
//
// "볼륨을 붙였는데 왜 아직도 경고가 뜨냐" 를 화면에서 끝낼 수 있어야 한다.
// 그래서 경고만 하지 않고 **지금 어디에 쓰고 있는지와 그렇게 판단한 근거**를
// 같이 보여 준다 — 마운트 경로를 /data 가 아닌 곳으로 잡은 경우가 대부분이다.
export default function StorageWarning({ storage, what, style }) {
  return (
    <div className="banner error" style={style}>
      <strong>{what}이 재배포하면 사라집니다.</strong>
      Railway 서비스에 Volume 을 추가하고 <strong>Mount path 를 정확히 <code>/data</code></strong> 로
      지정한 뒤 재배포하세요. 환경변수는 추가하지 않아도 됩니다.
      {storage?.path && (
        <div className="hint" style={{ marginTop: 6 }}>
          지금 쓰는 곳: <code>{storage.path}</code>
          {storage.note ? ` — ${storage.note}` : ''}
        </div>
      )}
    </div>
  )
}
