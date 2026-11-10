/** One trigger as the pane shows it. */
export type Trigger = {
  id: number
  when: string
  watches: string
  checked: string
  mode: 'ask first' | 'automatic'
  on: boolean
  fired: number
}

/** A kind of event a sentence may mean, as Ambient's suggest ranks it. */
export type Kind = { sig: string; app: string; label: string; picked: boolean }

/** A trigger drafted from one sentence, not saved yet. */
export type Draft = {
  intent: string
  kinds: Kind[]
  contentCheck: boolean
  checked: number
  matched: number
  samples: { text: string; passed: boolean }[]
}

declare module 'claude-code' {
  interface PluginState {
    'ambient-triggers': {
      triggers: Shaped<Trigger[]>
      draft: Shaped<Draft | null>
      note: string
    }
  }
}
