// Copyright (c) 2026, Ravindu Gajanayaka
// Licensed under GPLv3. See license.txt

import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import { call } from 'frappe-ui'
import { session } from './session'

export const usePosSessionStore = defineStore('posSession', () => {
  const openingEntry = ref<string | null>(null)
  const posProfile = ref<string>('')
  const company = ref<string>('')
  const isOpen = ref(false)
  const loading = ref(false)

  const hasOpenShift = computed(() => isOpen.value && !!openingEntry.value)

  async function resetClientState() {
    const [cart, customer, payment, drafts, items, settings] = await Promise.all([
      import('@/stores/cart'),
      import('@/stores/customer'),
      import('@/stores/payment'),
      import('@/stores/drafts'),
      import('@/stores/items'),
      import('@/stores/settings'),
    ])
    cart.useCartStore().$reset()
    customer.useCustomerStore().$reset()
    payment.usePaymentStore().$reset()
    drafts.useDraftsStore().$reset()
    items.useItemsStore().$reset()
    settings.useSettingsStore().$reset()
  }

  // Update desk page-head indicator with opened POS profile
  watch(posProfile, (name) => {
    if (name && typeof window.posPageSetProfile === 'function') {
      window.posPageSetProfile(name)
    } else if (!name && typeof window.posPageClearProfile === 'function') {
      window.posPageClearProfile()
    }
  })

  async function checkOpeningEntry() {
    loading.value = true
    try {
      // check_opening_entry returns a LIST of open POS Opening Entries
      const data = await call(
        'pos_prime.api.pos_session.check_opening_entry',
        { user: session.user.data }
      )
      const openVouchers = Array.isArray(data) ? data : []
      if (openVouchers.length > 0) {
        if (openingEntry.value && openingEntry.value !== openVouchers[0].name) {
          await resetClientState()
        }
        openingEntry.value = openVouchers[0].name
        posProfile.value = openVouchers[0].pos_profile
        company.value = openVouchers[0].company
        isOpen.value = true
      } else if (openingEntry.value) {
        await resetClientState()
        $reset()
      }
      return openVouchers
    } finally {
      loading.value = false
    }
  }

  async function createOpeningEntry(args: {
    pos_profile: string
    company: string
    balance_details: { mode_of_payment: string; opening_amount: number }[]
  }) {
    loading.value = true
    try {
      const data = await call(
        'pos_prime.api.pos_session.create_opening_entry',
        {
          pos_profile: args.pos_profile,
          company: args.company,
          balance_details: JSON.stringify(args.balance_details),
        }
      )
      if (data.name) {
        if (openingEntry.value !== data.name) await resetClientState()
        openingEntry.value = data.name
        posProfile.value = args.pos_profile
        company.value = args.company
        isOpen.value = true
      }
      return data
    } finally {
      loading.value = false
    }
  }

  async function closeShift(closingAmounts: {
    mode_of_payment: string
    closing_amount: number
  }[]) {
    loading.value = true
    try {
      const data = await call('pos_prime.api.pos_session.close_shift', {
        opening_entry: openingEntry.value,
        closing_amounts: closingAmounts,
      })
      await resetClientState()
      $reset()
      return data
    } finally {
      loading.value = false
    }
  }

  function $reset() {
    openingEntry.value = null
    posProfile.value = ''
    company.value = ''
    isOpen.value = false
    loading.value = false
  }

  return {
    openingEntry,
    posProfile,
    company,
    isOpen,
    loading,
    hasOpenShift,
    checkOpeningEntry,
    createOpeningEntry,
    closeShift,
    $reset,
  }
})
