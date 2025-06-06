import type { Callback } from '@bus/callbacks'

/**
 * send a message to the vscode extension.
 *
 * This should generally not be used directly, but rather through the useVSCode hook.
 */
export const sendVSCodeMessage = <K extends keyof Callback>(
  callbackName: K,
  payload: Callback[K],
): void => {
  const eventPayload = {
    key: callbackName,
    payload: payload,
  }
  console.log('sending message to self', eventPayload)
  getVSCodeAPI().postMessage(eventPayload)
}

let VSCODE_API: VSCodeAPI | undefined

interface VSCodeAPI {
  postMessage: (message: any) => void
}

function getVSCodeAPI(): VSCodeAPI {
  if (!VSCODE_API) {
    // @ts-ignore
    VSCODE_API = acquireVsCodeApi()
  }
  if (!VSCODE_API) {
    throw new Error('VSCode API not initialized')
  }
  return VSCODE_API
}
