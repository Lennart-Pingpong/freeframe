/**
 * An upload another tab is still sending.
 *
 * Rows are persisted to `localStorage`, which every tab of the origin shares, so
 * a second tab reads the first tab's in-flight rows. It used to rewrite every one
 * of them to `interrupted` and offer Discard, and the server accepted the discard,
 * because the version really was still uploading: the first tab's next part got
 * NoSuchUpload, and hours of transfer were gone from a row that said it had
 * already stopped.
 *
 * The same happens without a second tab. History rows for a version still
 * `uploading` come back as `interrupted` too, so a laptop showed a phone's live
 * upload the same way -- which is why the server's activity timestamp is asked as
 * well, and not only the heartbeat tabs leave for each other.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('@/lib/api', () => ({
  api: { post: vi.fn(), get: vi.fn() },
  ApiError: class ApiError extends Error {
    status: number
    detail: string
    constructor(status: number, detail: string) {
      super(detail)
      this.name = 'ApiError'
      this.status = status
      this.detail = detail
    }
  },
}))

import { api } from '@/lib/api'
import { useUploadStore, currentTabId, LIVE_WINDOW_MS } from '../upload-store'
import type { UploadFile } from '../upload-store'

const MB = 1024 * 1024
const VERSION_ID = 'version-1'
const ASSET_ID = 'asset-1'
const OTHER_TAB = 'some-other-tab'

function row(overrides: Partial<UploadFile> = {}): UploadFile {
  return {
    id: 'row-1', fileName: 'clip.mp4', fileSize: 23 * MB, fileType: 'video/mp4',
    projectId: 'project-1', assetName: 'clip', progress: 41, processingProgress: 0,
    status: 'uploading', assetId: ASSET_ID, versionId: VERSION_ID, uploadId: 'u1',
    createdAt: Date.now(),
    ...overrides,
  }
}

function rowOf(id: string) {
  return useUploadStore.getState().files.find((f) => f.id === id)!
}

/** What zustand's persist middleware does on the way in. */
function rehydrate(stored: UploadFile[]): UploadFile[] {
  const opts = (useUploadStore as unknown as {
    persist: { getOptions: () => { merge?: (p: unknown, c: unknown) => { files: UploadFile[] } } }
  }).persist.getOptions()
  return opts.merge!({ files: stored }, { files: [] }).files
}

function resumeInfo(lastActivity: Date | null) {
  return {
    state: 'resumable', upload_id: 'u1', s3_key: 'raw/p/a/v/original.mp4',
    asset_id: ASSET_ID, version_id: VERSION_ID, chunk_size_bytes: 10 * MB,
    file_size_bytes: 23 * MB, original_filename: 'clip.mp4', mime_type: 'video/mp4',
    held_part_numbers: [], last_activity_at: lastActivity?.toISOString() ?? null,
  }
}

const secondsAgo = (n: number) => new Date(Date.now() - n * 1000)

// ---------------------------------------------------------- reading a stored row

describe('a stored in-flight row, judged by the tab that loads it', () => {
  it('is another tab\'s live transfer while that tab keeps stamping it', () => {
    const [loaded] = rehydrate([row({ ownerTab: OTHER_TAB, heartbeatAt: Date.now() - 5_000 })])

    expect(loaded.status).toBe('elsewhere')
  })

  it('is interrupted once that tab has stopped stamping it', () => {
    const [loaded] = rehydrate([
      row({ ownerTab: OTHER_TAB, heartbeatAt: Date.now() - LIVE_WINDOW_MS - 1 }),
    ])

    expect(loaded.status).toBe('interrupted')
  })

  it('is interrupted straight away after this tab reloads, however fresh the stamp', () => {
    // The case the persisted rows exist for. The transfer died with the page,
    // and making the user wait out a window before Resume appears would be
    // the feature failing at the one moment it is for.
    const [loaded] = rehydrate([row({ ownerTab: currentTabId(), heartbeatAt: Date.now() })])

    expect(loaded.status).toBe('interrupted')
  })

  it('stays a running upload when it came from history, which has no upload id', () => {
    // Found by driving two tabs in a real browser: the history row for a
    // running upload was stored as Elsewhere, and the next tab to load it
    // turned it into "Failed -- Upload interrupted", with no way to resume.
    const [loaded] = rehydrate([
      row({ id: 'history-asset-1', status: 'elsewhere', uploadId: undefined, elsewhereUntil: Date.now() + 60_000 }),
    ])

    expect(loaded.status).toBe('elsewhere')
  })

  it('offers it as interrupted once the window it was held for has passed', () => {
    const [loaded] = rehydrate([
      row({ id: 'history-asset-1', status: 'elsewhere', uploadId: undefined, elsewhereUntil: Date.now() - 1 }),
    ])

    expect(loaded.status).toBe('interrupted')
  })

  it('is still failed when it never got an upload id', () => {
    const [loaded] = rehydrate([
      row({ ownerTab: OTHER_TAB, heartbeatAt: Date.now(), versionId: undefined, uploadId: undefined }),
    ])

    expect(loaded.status).toBe('failed')
  })
})

// ---------------------------------------------------------- noticing it stopped

describe('a row sent from elsewhere, while this tab watches', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    vi.mocked(api.get).mockResolvedValue(null as never)
  })

  /** What the sending tab last wrote to shared storage. */
  function otherTabWrote(heartbeatAt: number) {
    window.localStorage.setItem('ff-uploads', JSON.stringify({
      state: { files: [row({ ownerTab: OTHER_TAB, heartbeatAt })] }, version: 0,
    }))
  }

  it('stays elsewhere while the other tab is still stamping it', async () => {
    // Its own copy of the stamp is from when this tab loaded, long ago. What
    // counts is what the sending tab is writing now.
    useUploadStore.setState({
      files: [row({ status: 'elsewhere', ownerTab: OTHER_TAB, heartbeatAt: Date.now() - LIVE_WINDOW_MS * 2 })],
    })
    otherTabWrote(Date.now())

    await useUploadStore.getState().refreshProcessingItems()

    expect(rowOf('row-1').status).toBe('elsewhere')
  })

  it('becomes interrupted when the other tab has gone quiet', async () => {
    useUploadStore.setState({
      files: [row({ status: 'elsewhere', ownerTab: OTHER_TAB, heartbeatAt: Date.now() })],
    })
    otherTabWrote(Date.now() - LIVE_WINDOW_MS - 1)

    await useUploadStore.getState().refreshProcessingItems()

    expect(rowOf('row-1').status).toBe('interrupted')
  })

  it('stays elsewhere for the window the server named, which leaves no stamp here', async () => {
    // Another device sends no heartbeat into this browser's storage.
    useUploadStore.setState({
      files: [row({ status: 'elsewhere', elsewhereUntil: Date.now() + 60_000 })],
    })

    await useUploadStore.getState().refreshProcessingItems()

    expect(rowOf('row-1').status).toBe('elsewhere')
  })
})

// ---------------------------------------------------------- refusing to touch it

describe('discarding or resuming an upload that is still moving somewhere else', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.post).mockResolvedValue({} as never)
    global.fetch = vi.fn().mockResolvedValue({ ok: true, headers: { get: () => '"e"' } }) as never
  })

  it('does not discard a row this tab did not start when the server saw it move just now', async () => {
    useUploadStore.setState({ files: [row({ status: 'interrupted', ownerTab: undefined })] })
    vi.mocked(api.get).mockResolvedValue(resumeInfo(secondsAgo(20)) as never)

    const refused = await useUploadStore.getState().discardUpload('row-1')

    expect(refused).toMatch(/another tab or on another device/)
    expect(api.post).not.toHaveBeenCalledWith('/upload/abort', expect.anything())
    expect(rowOf('row-1').status).toBe('elsewhere')
  })

  it('discards it once the server has not seen it move for the whole window', async () => {
    useUploadStore.setState({ files: [row({ status: 'interrupted', ownerTab: undefined })] })
    vi.mocked(api.get).mockResolvedValue(
      resumeInfo(new Date(Date.now() - LIVE_WINDOW_MS - 1000)) as never,
    )

    await useUploadStore.getState().discardUpload('row-1')

    expect(api.post).toHaveBeenCalledWith('/upload/abort', expect.objectContaining({ discard: true }))
    expect(useUploadStore.getState().files).toEqual([])
  })

  it('discards this tab\'s own row without waiting, since this tab knows it stopped', async () => {
    // A network drop a few seconds ago is recent activity too. Refusing here
    // would lock the user out of their own upload for the length of the window.
    useUploadStore.setState({ files: [row({ status: 'interrupted', ownerTab: currentTabId() })] })
    vi.mocked(api.get).mockResolvedValue(resumeInfo(secondsAgo(5)) as never)

    await useUploadStore.getState().discardUpload('row-1')

    expect(api.post).toHaveBeenCalledWith('/upload/abort', expect.objectContaining({ discard: true }))
  })

  it('does not resume into an upload another tab is still sending parts into', async () => {
    // Two tabs PUTting the same part numbers leave the completing tab with
    // ETags that no longer match what is stored.
    useUploadStore.setState({ files: [row({ status: 'interrupted', ownerTab: undefined })] })
    vi.mocked(api.get).mockResolvedValue(resumeInfo(secondsAgo(20)) as never)

    useUploadStore.getState().resumeUpload('row-1', new File([new Uint8Array(10)], 'clip.mp4'))
    await vi.waitFor(() => expect(rowOf('row-1').status).toBe('elsewhere'))

    expect(api.post).not.toHaveBeenCalledWith('/upload/presign-part', expect.anything())
    expect(global.fetch).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------- the second attempt

describe('after a refusal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.post).mockResolvedValue({} as never)
  })

  it('holds the row only until the server stops counting the upload as live', async () => {
    // Held for a fresh window from the refusal, the row kept saying Elsewhere
    // for up to two and a half minutes after a second attempt could already
    // have worked -- which, with a refusal that also renewed the activity it
    // refused on, read as a resume that never became possible.
    useUploadStore.setState({ files: [row({ status: 'interrupted', ownerTab: undefined })] })
    const lastMoved = secondsAgo(100)
    vi.mocked(api.get).mockResolvedValue(resumeInfo(lastMoved) as never)

    await useUploadStore.getState().discardUpload('row-1')

    expect(rowOf('row-1').elsewhereUntil).toBe(lastMoved.getTime() + LIVE_WINDOW_MS)
  })
})

// ---------------------------------------------------------- history from another device

describe('an upload that only history knows about', () => {
  function assetUploading(lastActivity: Date) {
    return {
      id: 'asset-9', project_id: 'project-1', name: 'clip', asset_type: 'video',
      latest_version: {
        id: 'version-9', asset_id: 'asset-9', version_number: 1,
        processing_status: 'uploading', created_by: 'u', created_at: new Date().toISOString(),
        deleted_at: null, last_activity_at: lastActivity.toISOString(), files: [],
      },
    }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    useUploadStore.setState({ files: [], historyLoaded: false })
  })

  it('is shown as running elsewhere while the other device is still sending it', async () => {
    // It came back as Interrupted, with Resume and Discard, while another
    // browser was still uploading it.
    vi.mocked(api.get).mockResolvedValue([assetUploading(secondsAgo(20))] as never)

    await useUploadStore.getState().fetchHistory()

    expect(useUploadStore.getState().files[0].status).toBe('elsewhere')
  })

  it('is interrupted once it has not moved for the whole window', async () => {
    vi.mocked(api.get).mockResolvedValue([assetUploading(secondsAgo(600))] as never)

    await useUploadStore.getState().fetchHistory()

    expect(useUploadStore.getState().files[0].status).toBe('interrupted')
  })
})

// ---------------------------------------------------------- stamping its own

describe('a tab that is sending', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useUploadStore.setState({ files: [] })
  })

  it('stamps the row with every finished part, not only on its timer', async () => {
    // A hidden tab's timers run about once a minute and its requests do not,
    // so on the timer alone the stamp fell behind the transfer.
    let clock = 1_000_000
    const now = vi.spyOn(Date, 'now').mockImplementation(() => (clock += 1000))
    vi.mocked(api.post).mockImplementation((path: string) => {
      if (path === '/upload/initiate') {
        return Promise.resolve({
          upload_id: 'u1', s3_key: 'raw/k', asset_id: ASSET_ID,
          version_id: VERSION_ID, chunk_size_bytes: 10 * MB,
        }) as never
      }
      if (path === '/upload/presign-part') {
        return Promise.resolve({ presigned_url: 'https://s3.example/p' }) as never
      }
      return new Promise(() => {}) as never // completion never answers
    })
    global.fetch = vi.fn().mockResolvedValue({ ok: true, headers: { get: () => '"e"' } }) as never

    const id = useUploadStore.getState().startUpload(
      new File([new Uint8Array(10)], 'clip.mp4', { type: 'video/mp4' }), 'project-1', 'clip',
    )
    try {
      const claimed = rowOf(id).heartbeatAt!
      await vi.waitFor(() => expect(rowOf(id).progress).toBeGreaterThan(0))

      expect(rowOf(id).heartbeatAt!).toBeGreaterThan(claimed)
    } finally {
      now.mockRestore()
    }
  })

  it('claims the row and stamps it', async () => {
    vi.mocked(api.post).mockImplementation(() => new Promise(() => {}) as never)

    const id = useUploadStore.getState().startUpload(
      new File([new Uint8Array(10)], 'clip.mp4', { type: 'video/mp4' }), 'project-1', 'clip',
    )

    expect(rowOf(id).ownerTab).toBe(currentTabId())
    expect(rowOf(id).heartbeatAt).toBeGreaterThan(Date.now() - 1000)
  })
})

// ---------------------------------------------------------- cancel

describe('cancelling an upload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useUploadStore.setState({ files: [], versionsRevision: 0 })
    vi.mocked(api.post).mockImplementation((path: string) => {
      if (path === '/upload/initiate') {
        return Promise.resolve({
          upload_id: 'u1', s3_key: 'raw/k', asset_id: ASSET_ID,
          version_id: VERSION_ID, chunk_size_bytes: 10 * MB,
        }) as never
      }
      if (path === '/upload/presign-part') {
        return Promise.resolve({ presigned_url: 'https://s3.example/p' }) as never
      }
      return Promise.resolve({}) as never
    })
    // A part that only ends when it is aborted.
    global.fetch = vi.fn((_: string, init?: RequestInit) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () =>
        reject(new DOMException('Aborted', 'AbortError')))
    })) as never
  })

  it('throws the upload away rather than recording a failure, and tells the version list', async () => {
    // A plain abort left the version `failed`: a red badge in the switcher for
    // an upload somebody stopped on purpose. And nothing told the switcher,
    // which went on saying "Uploading" until a reload.
    const id = useUploadStore.getState().startUpload(
      new File([new Uint8Array(10)], 'clip.mp4', { type: 'video/mp4' }), 'project-1', 'clip',
    )
    await vi.waitFor(() => expect(global.fetch).toHaveBeenCalled())

    useUploadStore.getState().cancelUpload(id)

    await vi.waitFor(() => expect(useUploadStore.getState().versionsRevision).toBe(1))
    expect(api.post).toHaveBeenCalledWith('/upload/abort', {
      s3_key: 'raw/k', upload_id: 'u1', version_id: VERSION_ID, discard: true,
    })
  })
})
