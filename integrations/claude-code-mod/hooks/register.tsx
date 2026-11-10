// Ambient's triggers in a pane beside the chat: the current triggers (polled from the local app), a switch
// on each, and one "Watch for…" field that drafts a trigger from a sentence and saves it once you confirm.
// Everything Ambient-specific goes through ./ambient.ts.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Draft, Trigger } from '../types'
import { ambient, DEFAULT_URL } from './ambient'

const PANE = 'ambient'
const TITLE = 'Ambient triggers'
const POLL_MS = 15_000 // politely: the app's own window polls faster, and nothing here is urgent
// Change a tag when that value's fields change: it declines what an earlier version stored.
const triggers = atom({ plugin: 'ambient-triggers', key: 'triggers' } as const, [], { shape: 'triggers-1' })
const draft = atom({ plugin: 'ambient-triggers', key: 'draft' } as const, null, { shape: 'draft-1' })
const note = atom({ plugin: 'ambient-triggers', key: 'note' } as const, '')

let base = DEFAULT_URL
let timer: { cancel: () => void } | undefined
let last = ''
const OFFLINE = () => `Ambient isn't answering at ${base}. Is the app running?`

function api($: EngineInterface) {
  return ambient((url, init) => $.http.fetch(url, init), base)
}

// Fetch the list; redraw only when something changed.
async function refresh($: EngineInterface) {
  try {
    const list: Trigger[] = await api($).triggers()
    const key = JSON.stringify(list)
    if (key !== last) {
      last = key
      await update($, triggers, () => list)
    }
    if ((await read($, note)) === OFFLINE()) await update($, note, () => '')
  } catch {
    await update($, note, () => OFFLINE())
  }
}

async function open($: EngineInterface, focus = false) {
  const placed = await $.ui.open({ id: PANE, title: TITLE, ...(focus ? { focus: true } : {}) })
  await refresh($)
  timer?.cancel()
  timer = $.clock.every(POLL_MS, async () => {
    const panes = await $.ui.panes()
    if (panes.some(p => p.id === PANE && p.isPlaced)) await refresh($)
  })
  return placed
}

export const register: Register = (on, options) => {
  base = typeof options.ambientUrl === 'string' && options.ambientUrl ? options.ambientUrl : DEFAULT_URL

  on('session.start', async ($, e, next) => {
    base = (await $.env.get('AMBIENT_URL')) || base // the same override the MCP server reads
    await $.command.register({ name: 'ambient', description: "Show Ambient's triggers beside the chat" })
    await refresh($) // before the first draw (drawing is pure: it only reads), quick on a local socket
    void open($)
    return next(e)
  })

  on('command.run', { command: 'ambient' }, async $ => {
    const r = await open($, true) // asked for: take the keys, so "Watch for…" is one Tab away
    return { text: r.isPlaced ? 'Ambient triggers pane opened.' : `Ambient pane is waiting: ${r.reason}` }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const list = await read($, triggers)
    const d = await read($, draft)
    const msg = await read($, note)

    const toggle = (t: Trigger) => async () => {
      const err = await api($).setEnabled(t.id, !t.on)
      await update($, note, () => err ?? `${t.on ? 'Off' : 'On'}: when ${t.when}`)
      await refresh($)
    }
    const suggest = async (text: string) => {
      const intent = text.trim()
      if (intent.length < 4) return
      await update($, note, () => 'Reading that on this device…')
      try {
        const got = await api($).draft(intent)
        await update($, draft, () => got)
        await update($, note, () => '')
      } catch {
        await update($, note, () => OFFLINE())
      }
    }
    const pick = (sig: string) => async () => {
      if (!d) return
      const sigs = d.kinds.map(k => (k.sig === sig ? { ...k, picked: !k.picked } : k)).filter(k => k.picked).map(k => k.sig)
      const got = await api($).draft(d.intent, sigs)
      await update($, draft, () => got)
    }
    const save = (cur: Draft) => async () => {
      const r = await api($).save(cur)
      if (r.error) return void (await update($, note, () => r.error!))
      await update($, draft, () => null)
      await update($, note, () => `Saved: when ${cur.intent}`)
      await refresh($)
    }

    const active = list.filter(t => t.on).length
    const picked = d ? d.kinds.filter(k => k.picked) : []

    return (
      <Box flexDirection="column" gap={1}>
        <Text dimColor>
          {list.length ? `${active} on · ${list.length - active} off` : 'No triggers yet.'}
        </Text>

        {list.map(t => (
          <Box key={`t${t.id}`} flexDirection="column">
            <Box flexDirection="row" gap={1}>
              <Button key={`on${t.id}`} label={t.on ? 'on ' : 'off'} variant={t.on ? 'primary' : 'secondary'} onPress={toggle(t)} />
              <Text bold={t.on} dimColor={!t.on} wrap="wrap">
                When {t.when}
              </Text>
            </Box>
            <Text dimColor wrap="truncate-end">
              {t.watches} · {t.checked} · {t.mode}
              {t.fired ? ` · fired ${t.fired}×` : ''}
            </Text>
          </Box>
        ))}

        <Input key="watch" label="Watch for…" placeholder="someone asks me for a file" submitLabel="suggest" onSubmit={suggest} />

        {d && (
          <Box flexDirection="column" borderStyle="round" paddingX={1}>
            <Text bold>When {d.intent}</Text>
            {d.kinds.length === 0 && <Text dimColor>Nothing like that seen yet. Do it once in the app, then try again.</Text>}
            {d.kinds.map(k => (
              <Button key={`k${k.sig}`} plain onPress={pick(k.sig)}>
                {k.picked ? '[x] ' : '[ ] '}
                {k.app} · {k.label}
              </Button>
            ))}
            {d.kinds.length > 0 && picked.length === 0 && <Text dimColor>Not sure which you mean: pick one.</Text>}
            {d.contentCheck && <Text dimColor>Content checked on this device</Text>}
            {picked.length > 0 && (
              <Text dimColor>
                {d.checked
                  ? `Backtest: would have fired ${d.matched} of the last ${d.checked}`
                  : 'No past activity of this kind to test against yet'}
              </Text>
            )}
            {d.samples.filter(s => s.passed).map((s, i) => (
              <Text key={`s${i}`} dimColor wrap="truncate-end">
                ✓ {s.text}
              </Text>
            ))}
            <Box flexDirection="row" gap={1}>
              {picked.length > 0 && <Button key="save" label="Save trigger" variant="primary" onPress={save(d)} />}
              <Button key="cancel" label="Cancel" onPress={() => update($, draft, () => null)} />
            </Box>
          </Box>
        )}

        {msg !== '' && <Text dimColor>{msg}</Text>}
      </Box>
    )
  })
}
