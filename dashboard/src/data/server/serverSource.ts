// 서버 어댑터: 실제 NearPilot 서버의 조회 REST(FR-12A)와 모니터링 WebSocket(FR-12B)을 DataSource로 보인다.
// 서버 메시지를 화면 모델로 옮기는 일만 한다 (map.ts, time.ts). 서버에는 읽기만 한다: GET과 스트림의 인증 메시지 말고는 보내지 않는다.
import type { StreamEvent } from '../../model/events';
import type { DataSource } from '../source';
import { approvalsOf, auditOf, healthOf, historyOf, mapEvent, placesOf, rentalOf, requestOf, serverPeriod, trustOf, usesOf } from './map';
import { formatLocal } from './time';

export interface ServerOptions {
  /** 기본은 location.origin. 시험이 바꾼다 */
  origin?: string;
  token: string;
  /** 토큰이 거부됐을 때 (REST 401·403, 스트림 4401·4403). 이유는 'invalid' | 'not_owner' */
  onRejected(reason: 'invalid' | 'not_owner'): void;
  /** 시험용 주입 */
  fetch?: typeof fetch;
  WebSocket?: typeof WebSocket;
  setTimeout?: typeof setTimeout;
  clearTimeout?: typeof clearTimeout;
}

/** 스트림 메시지 형식의 버전 (architecture.md §5 "WebSocket"의 v). 다르면 읽지 않고 저절로 다시 연결하지도 않는다. 사람이 누른 다시 연결만 새로 연결한다 */
const PROTOCOL_VERSION = 1;
/** 닫기 코드 (architecture.md §5): 토큰 오류와 소유자 아님은 다시 연결하지 않는다. 4408, 1011, 1013, 그 밖의 끊김은 다시 연결한다 */
const CLOSE_INVALID_TOKEN = 4401;
const CLOSE_NOT_OWNER = 4403;
/** 다시 연결하기 전에 기다리는 시간: 1초부터 두 배씩, 최대 10초. 표시용 값이라 서버 설정이 아니다 */
const RETRY_FIRST_MS = 1000;
const RETRY_MAX_MS = 10_000;

/** mock은 정의하지 않는다: 서버 데이터에 목 데이터 표시(상단 바)를 붙이지 않는다 */
export function createServerSource(options: ServerOptions): DataSource {
  const { token, onRejected } = options;
  const origin = options.origin ?? location.origin;
  /** 지금 열려 있는 구독마다 하나. reconnect()가 이 연결들의 기다리는 재연결을 바로 한다 */
  const connections = new Set<{ retryNow(): void }>();

  /** 조회 REST 한 번 (GET). 오류 문구에는 토큰이 들어가지 않는다 */
  async function get(path: string, params: Record<string, string> = {}, notFound?: string): Promise<unknown> {
    const url = new URL(path, origin);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
    let res: Response;
    try {
      res = await (options.fetch ?? fetch)(url.toString(), { method: 'GET', headers: { Authorization: `Bearer ${token}` } });
    } catch {
      // 브라우저의 오류 문구에는 요청 헤더 내용이 들어갈 수 있어 원문을 쓰지 않는다
      throw new Error('서버에 연결하지 못했어요');
    }
    if (res.status === 401 || res.status === 403) {
      onRejected(res.status === 401 ? 'invalid' : 'not_owner');
      throw new Error('토큰이 거부됐어요');
    }
    if (res.status === 404 && notFound) throw new Error(notFound);
    if (!res.ok) {
      const body: unknown = await res.json().catch(() => undefined);
      const code = typeof body === 'object' && body !== null && 'error' in body && typeof body.error === 'string' ? body.error : '';
      throw new Error(`서버 오류 ${res.status}${code ? ` · ${code}` : ''}`);
    }
    try {
      return await res.json();
    } catch {
      throw new Error('서버 응답을 읽지 못했어요');
    }
  }

  /** 스트림 연결 하나. 해제하면 소켓을 닫고 재연결 타이머를 지운다 */
  function connect(listener: (events: StreamEvent[]) => void) {
    const url = new URL('/dashboard/stream', origin);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket: WebSocket | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    /** 연결이 끊긴 뒤 다시 연결을 예약한 수. 연결이 열리면 0이다 */
    let attempts = 0;
    /** 메시지의 v가 달라 연결을 끊고 멈춘 상태. 저절로 다시 연결하지 않고, 사람이 누른 다시 연결(retryNow)만 새로 연다 */
    let versionMismatch = false;

    const link = (connected: boolean) => listener([{ type: 'link', at: formatLocal(new Date(), 'HH:MM:SS.mmm'), connected, reconnectAttempts: attempts }]);

    /** 메시지 하나를 한 묶음으로 넘긴다 */
    const onMessage = (data: unknown) => {
      if (typeof data !== 'string') return;
      let msg: unknown;
      try {
        msg = JSON.parse(data);
      } catch {
        return;
      }
      if (typeof msg !== 'object' || msg === null || Array.isArray(msg)) return;
      const m = msg as { v?: unknown; events?: unknown };
      if (m.v !== undefined && m.v !== PROTOCOL_VERSION) {
        // 읽을 수 없는 형식이다. 연결을 끊고 저절로 다시 연결하지 않는다
        const old = socket;
        socket = undefined;
        versionMismatch = true;
        old?.close(1000);
        link(false);
        return;
      }
      if (m.v !== PROTOCOL_VERSION || !Array.isArray(m.events)) return;
      listener(m.events.flatMap((e) => mapEvent(e) ?? []));
    };

    const onClose = (code: number) => {
      socket = undefined;
      if (code === CLOSE_INVALID_TOKEN || code === CLOSE_NOT_OWNER) {
        link(false);
        onRejected(code === CLOSE_INVALID_TOKEN ? 'invalid' : 'not_owner');
        return;
      }
      attempts += 1;
      link(false);
      timer = (options.setTimeout ?? setTimeout)(open, Math.min(RETRY_MAX_MS, RETRY_FIRST_MS * 2 ** (attempts - 1)));
    };

    function open() {
      timer = undefined;
      versionMismatch = false;
      const ws = new (options.WebSocket ?? WebSocket)(url.toString());
      socket = ws;
      // 해제했거나 닫은 소켓의 늦은 이벤트는 무시한다
      ws.onopen = () => {
        if (socket !== ws) return;
        attempts = 0;
        ws.send(JSON.stringify({ type: 'auth', token }));
        link(true);
      };
      ws.onmessage = (e) => { if (socket === ws) onMessage(e.data); };
      ws.onclose = (e) => { if (socket === ws) onClose(e.code); };
    }

    open();
    return {
      /** 기다리는 재연결을 바로 하고, v 불일치로 멈춘 연결은 새로 연다. 연결이 열려 있거나 4401·4403으로 멈췄으면 아무것도 하지 않는다 */
      retryNow() {
        if (timer === undefined && !versionMismatch) return;
        if (timer !== undefined) (options.clearTimeout ?? clearTimeout)(timer);
        open();
      },
      close() {
        if (timer !== undefined) (options.clearTimeout ?? clearTimeout)(timer);
        timer = undefined;
        const old = socket;
        socket = undefined;
        old?.close();
      }
    };
  }

  return {
    // 구독마다 연결 하나를 연다 (React StrictMode가 effect를 두 번 돌려도 해제한 연결은 닫힌다)
    subscribe(listener) {
      const c = connect(listener);
      connections.add(c);
      return () => { connections.delete(c); c.close(); };
    },
    reconnect() {
      connections.forEach((c) => c.retryNow());
    },
    places: () => get('/dashboard/places').then(placesOf),
    uses: () => get('/dashboard/uses').then(usesOf),
    approvals: () => get('/dashboard/approvals').then(approvalsOf),
    audit: () => get('/dashboard/audit').then(auditOf),
    history: (q) => get('/dashboard/history', { period: serverPeriod(q.period), ...(q.verdict ? { verdict: q.verdict } : {}), ...(q.nodeId ? { nodeId: q.nodeId } : {}) }).then(historyOf),
    request: (id) => get(`/dashboard/requests/${encodeURIComponent(id)}`, {}, `요청 ${id} 없음`).then(requestOf),
    trust: () => get('/dashboard/trust').then(trustOf),
    rental: (nodeId) => get(`/dashboard/rentals/${encodeURIComponent(nodeId)}`, {}, `대여 ${nodeId} 없음`).then(rentalOf),
    health: () => get('/dashboard/health').then(healthOf)
  };
}
