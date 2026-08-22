// Copyright (c) 2026, Ravindu Gajanayaka
// Licensed under GPLv3. See license.txt

import { ref } from 'vue'

export type TerminalStatus = 'disconnected' | 'connecting' | 'connected' | 'processing' | 'success' | 'error'

export function usePaymentTerminal() {
  const status = ref<TerminalStatus>('disconnected')
  const error = ref<string | null>(null)
  const paymentReference = ref<string | null>(null)

  async function connect() {
    status.value = 'disconnected'
    error.value = 'No verified payment terminal is configured'
    return false
  }

  async function collectPayment(_amount: number, _currency: string): Promise<{ success: boolean; reference: string | null }> {
    status.value = 'error'
    error.value = 'Card payment is unavailable until a verified terminal is configured'
    paymentReference.value = null
    return { success: false, reference: null }
  }

  function cancelPayment() {
    status.value = 'disconnected'
  }

  return { status, error, paymentReference, connect, collectPayment, cancelPayment }
}
