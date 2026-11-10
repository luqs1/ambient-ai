// The one module that knows Ambient's HTTP API (today's: /api/triggers, /suggest, /preview, POST /api/triggers).
// When the trigger engine changes shape, only this file changes. Veil and capture are deliberately not here.
import type { HttpInit, HttpResponse } from 'claude-code'

import type { Draft, Kind, Trigger } from '../types'

export type Fetch = (url: string, init?: HttpInit) => Promise<HttpResponse>
export const DEFAULT_URL = 'http://127.0.0.1:8787'
const THEN = 'Tell me in one line what happened and the smallest next step.'

type Row = Record<string, unknown> & { id: number; sigs?: string[]; kinds?: { app?: string; label?: string }[] }

export function ambient(fetch: Fetch, base: string = DEFAULT_URL) {
  const root = base.replace(/\/+$/, '')
  const call = async (path: string, body?: unknown): Promise<any> => {
    const init: HttpInit = body === undefined ? {} : {
      method: 'POST',
      // Writes need X-Ambient and a JSON type; no Origin is sent, so the API's local-only check passes.
      headers: { 'content-type': 'application/json', 'X-Ambient': '1' },
      body: JSON.stringify(body),
    }
    const r = await fetch(root + path, init)
    const data = r.text ? JSON.parse(r.text) : null
    if (!r.ok && !(data && data.error)) throw new Error(`Ambient answered ${r.status}`)
    return data
  }
  const rows = async (): Promise<Row[]> => (await call('/api/triggers'))?.triggers ?? []

  return {
    url: root,

    async triggers(): Promise<Trigger[]> {
      return (await rows()).map(t => ({
        id: t.id,
        when: String(t.text || (t.kinds ?? []).map(k => k.label).join(' or ')),
        watches: (t.kinds ?? []).map(k => `${k.app} · ${k.label}`).join(', '),
        checked: t.semantic ? 'content checked on this device' : 'every time',
        mode: t.auto ? 'automatic' : 'ask first',
        on: Boolean(t.enabled),
        fired: Number(t.fired ?? 0),
      }))
    },

    async setEnabled(id: number, on: boolean): Promise<string | undefined> {
      const t = (await rows()).find(r => r.id === id)
      if (!t) return `No trigger ${id}`
      const keep = ['id', 'name', 'text', 'sigs', 'action', 'instruction', 'auto', 'condition', 'threshold', 'semantic', 'agent']
      const r = await call('/api/triggers', { ...Object.fromEntries(keep.map(k => [k, t[k]])), enabled: on })
      return r?.error
    },

    async draft(intent: string, sigs?: string[]): Promise<Draft> {
      const s = (await call('/api/triggers/suggest', { text: intent })) ?? {}
      const kinds: Kind[] = (s.kinds ?? []).map((k: any) => ({
        sig: k.sig, app: k.app ?? '', label: k.label ?? k.sig,
        picked: sigs ? sigs.includes(k.sig) : Boolean(k.picked),
      }))
      const picked = kinds.filter(k => k.picked).map(k => k.sig)
      const contentCheck = Boolean(s.about_content)
      const p = picked.length
        ? (await call('/api/triggers/preview', { text: intent, sigs: picked, semantic: contentCheck, threshold: 0.5 })) ?? {}
        : {}
      return {
        intent, kinds, contentCheck,
        checked: Number(p.checked ?? 0), matched: Number(p.matched ?? 0),
        samples: (p.samples ?? []).slice(0, 3).map((x: any) => ({ text: String(x.text || x.label || ''), passed: Boolean(x.passed) })),
      }
    },

    async save(d: Draft): Promise<{ id?: number; error?: string }> {
      const sigs = d.kinds.filter(k => k.picked).map(k => k.sig)
      if (!sigs.length) return { error: 'Pick what to watch first' }
      return call('/api/triggers', {
        text: d.intent, sigs, semantic: d.contentCheck, threshold: 0.5, action: 'ask', instruction: THEN,
        name: d.intent.slice(0, 60), auto: false, enabled: true,
      })
    },
  }
}
