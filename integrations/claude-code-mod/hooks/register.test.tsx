import { expect, test } from 'claude-code/testing'

// A stand-in for Ambient's local API: today's trigger routes, in memory.
type Row = Record<string, unknown> & { id: number; enabled: number }

function fakeAmbient(on: any) {
  const rows: Row[] = [
    { id: 1, name: 'Draft a reply', text: 'someone DMs me', sigs: ['*:dm'], action: 'draft', instruction: '', auto: 0,
      enabled: 1, semantic: 0, fired: 2, kinds: [{ sig: '*:dm', app: 'your chat apps', label: 'someone DMs me' }] },
  ]
  const posts: { url: string; body: any }[] = []
  on('http.fetch', (_: unknown, e: { url: string; init?: { method?: string; body?: string; headers?: Record<string, string> } }) => {
    const path = new URL(e.url).pathname
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    const ok = (v: unknown) => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(v) } })
    if (body !== undefined) {
      posts.push({ url: e.url, body })
      if (e.init?.headers?.['X-Ambient'] !== '1') return { value: { status: 403, ok: false, headers: {}, text: '{"error":"missing X-Ambient header"}' } }
    }
    if (path === '/api/triggers' && !body) return ok({ triggers: rows })
    if (path === '/api/triggers/suggest')
      return ok({ kinds: [{ sig: '*:dm', app: 'your chat apps', label: 'someone DMs me', picked: true },
                          { sig: 'slack:dm', app: 'Slack', label: 'someone DMs me', picked: false }], about_content: true, sure: true })
    if (path === '/api/triggers/preview') return ok({ checked: 6, matched: 3, samples: [{ text: 'send me the deck?', passed: true }] })
    if (path === '/api/triggers') {
      if (body.id) Object.assign(rows.find(r => r.id === body.id)!, { enabled: body.enabled ? 1 : 0 })
      else rows.push({ ...body, id: rows.length + 1, enabled: 1, fired: 0, kinds: [] })
      return ok({ ok: true, id: body.id ?? rows.length })
    }
    return { value: { status: 404, ok: false, headers: {}, text: '{"error":"not found"}' } }
  })
  return { rows, posts }
}

const PANE = {
  plugin: 'ambient-triggers',
  component: 'Pane',
  requestId: 'ambient',
  props: { title: 'Ambient triggers', isFocused: false, bodyColumns: 50, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const

function engine(on: any) {
  on('command.register', () => ({ value: { command: 'ambient' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('clock.every', () => ({ value: undefined }))
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('env.get', () => ({ value: undefined }))
}

test('lists the triggers and switches one off, on every surface', async ($, on) => {
  engine(on)
  const amb = fakeAmbient(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const was = amb.rows[0]!.enabled
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /When someone DMs me/ }))?.text).toBe('When someone DMs me')
    expect(await ui.find({ type: 'Text', text: /fired 2×/ })).toBeTruthy()
    await ui.press({ key: 'on1' })
    expect(amb.rows[0]!.enabled).toBe(was ? 0 : 1)
    expect(amb.posts.at(-1)!.body).toMatchObject({ id: 1, enabled: !was, sigs: ['*:dm'], action: 'draft' })
    await ui.unmount()
  }
})

test('watch for: suggests, backtests, and saves only on Save', async ($, on) => {
  engine(on)
  const amb = fakeAmbient(on)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })

  await ui.input({ key: 'watch', text: 'someone asks me for a file' })
  expect(await ui.find({ type: 'Text', text: /would have fired 3 of the last 6/ })).toBeTruthy()
  expect(amb.rows).toHaveLength(1)                      // a draft is not a trigger

  await ui.press({ key: 'save' })
  expect(amb.rows).toHaveLength(2)
  expect(amb.posts.at(-1)!.body).toMatchObject({ text: 'someone asks me for a file', sigs: ['*:dm'], semantic: true, auto: false })
  expect(await ui.find({ type: 'Text', text: /Saved: when someone asks me for a file/ })).toBeTruthy()
  await ui.unmount()
})
