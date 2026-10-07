/**
 * A stand-in for XMLHttpRequest, for the part PUTs of the upload store.
 *
 * jsdom's own XHR sends a Blob without any upload progress a test could use,
 * and would try to reach the presigned URL. This one hands each request to a
 * handler instead, which plays the storage backend: it answers with a reply,
 * or rejects for a network error, and may report bytes on the way through
 * `request.progress()`. A handler that never settles is a part still in
 * flight, which only an abort ends.
 *
 * Only what the store uses is modelled, plus one thing a browser does on its
 * own: a Blob body with a type is sent with that type as its Content-Type, so a
 * test can see a header the code never set itself.
 */
import { vi } from 'vitest'

export interface PartReply {
  status: number
  statusText?: string
  etag?: string
}

export type PartHandler = (url: string, request: FakeXhr) => PartReply | Promise<PartReply>

export const ok = (etag: string): PartReply => ({ status: 200, etag })
export const fail = (status = 503, statusText = 'Service Unavailable'): PartReply => ({ status, statusText })

export class FakeXhr {
  static handler: PartHandler = () => new Promise(() => {})

  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  ontimeout: (() => void) | null = null
  onabort: (() => void) | null = null

  method = ''
  url = ''
  body: Blob | null = null
  /** Lower-cased, as they would go over the wire. */
  requestHeaders: Record<string, string> = {}
  status = 0
  statusText = ''
  /** Whether the request ended by `abort()`, rather than by a reply or an error. */
  aborted = false

  private sent = false
  private settled = false
  private etag: string | null = null

  open(method: string, url: string): void {
    this.method = method
    this.url = url
  }

  setRequestHeader(name: string, value: string): void {
    this.requestHeaders[name.toLowerCase()] = value
  }

  getResponseHeader(name: string): string | null {
    return name.toLowerCase() === 'etag' ? this.etag : null
  }

  send(body: Blob | null): void {
    this.body = body
    this.sent = true
    if (body instanceof Blob && body.type && !('content-type' in this.requestHeaders)) {
      this.requestHeaders['content-type'] = body.type
    }
    Promise.resolve()
      .then(() => FakeXhr.handler(this.url, this))
      .then(
        (reply) => this.reply(reply),
        () => this.networkError(),
      )
  }

  abort(): void {
    if (!this.sent || this.settled) return
    this.settled = true
    this.aborted = true
    this.onabort?.()
  }

  /** Reports `loaded` bytes of the body as sent. */
  progress(loaded: number): void {
    if (this.settled) return
    const total = this.body?.size ?? 0
    this.upload.onprogress?.({ loaded, total, lengthComputable: true } as ProgressEvent)
  }

  timeout(): void {
    if (this.settled) return
    this.settled = true
    this.ontimeout?.()
  }

  private reply({ status, statusText = '', etag }: PartReply): void {
    if (this.settled) return
    this.settled = true
    this.status = status
    this.statusText = statusText
    this.etag = etag ?? null
    this.onload?.()
  }

  private networkError(): void {
    if (this.settled) return
    this.settled = true
    this.onerror?.()
  }
}

/**
 * Puts the fake in place of XMLHttpRequest and routes every request to
 * `handler`. Returns the requests in the order they were opened.
 */
export function installXhrFake(handler: PartHandler): FakeXhr[] {
  const requests: FakeXhr[] = []
  FakeXhr.handler = handler
  vi.stubGlobal(
    'XMLHttpRequest',
    class extends FakeXhr {
      constructor() {
        super()
        requests.push(this)
      }
    },
  )
  return requests
}
