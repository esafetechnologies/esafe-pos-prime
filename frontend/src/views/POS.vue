<!-- Copyright (c) 2026, Ravindu Gajanayaka -->
<!-- Licensed under GPLv3. See license.txt -->

<script setup lang="ts">
import { ref, onMounted, watch, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { usePosSessionStore } from '@/stores/posSession'
import { useSettingsStore } from '@/stores/settings'
import { useCartStore } from '@/stores/cart'
import { useCustomerStore } from '@/stores/customer'
import { usePaymentStore } from '@/stores/payment'
import { useDraftsStore } from '@/stores/drafts'
import { useItemsStore } from '@/stores/items'
import { useKeyboardShortcuts } from '@/composables/useKeyboardShortcuts'
import { useBroadcastDisplay, type DisplayMessage } from '@/composables/useBroadcastDisplay'
import { call } from 'frappe-ui'
import { useSerialDisplay } from '@/composables/useSerialDisplay'
import { useDeskMode } from '@/composables/useDeskMode'
import AppShell from '@/components/layout/AppShell.vue'
import ItemGrid from '@/components/items/ItemGrid.vue'
import Cart from '@/components/cart/Cart.vue'
import PaymentDialog from '@/components/payment/PaymentDialog.vue'
import ReceiptPreview from '@/components/receipt/ReceiptPreview.vue'
import HeldOrdersDrawer from '@/components/orders/HeldOrdersDrawer.vue'
import ReturnSearchDialog from '@/components/orders/ReturnSearchDialog.vue'
import { LayoutGrid, ShoppingCart } from 'lucide-vue-next'

const router = useRouter()
const sessionStore = usePosSessionStore()
const settingsStore = useSettingsStore()
const cartStore = useCartStore()
const customerStore = useCustomerStore()
const paymentStore = usePaymentStore()
const draftsStore = useDraftsStore()
const itemsStore = useItemsStore()

// Customer display — scoped by POS Opening Entry so multiple sessions don't conflict
const { sendUpdate: sendDisplayUpdate, onUpdate: onDisplayMessage, close: closeDisplay } = useBroadcastDisplay(sessionStore.openingEntry || undefined)
const serialDisplay = useSerialDisplay()
const { isDeskMode } = useDeskMode()

const mobileTab = ref<'items' | 'cart'>('items')
const showReceipt = ref(false)
const showHeldOrders = ref(false)
const showReturnDialog = ref(false)

// Resizable cart panel (percentage-based, persisted)
const CART_MIN_PCT = 25
const CART_MAX_PCT = 50
const CART_DEFAULT_PCT = 40  // ERPNext default: 4/10 = 40%
const cartPct = ref(parseInt(localStorage.getItem('pos_cart_pct') || '') || CART_DEFAULT_PCT)

function onResizeStart(e: PointerEvent) {
  const target = e.currentTarget as HTMLElement
  target.setPointerCapture(e.pointerId)
  const container = target.closest('.pos-layout') as HTMLElement
  if (!container) return
  const containerWidth = container.clientWidth
  const startX = e.clientX
  const startPct = cartPct.value

  function onMove(ev: PointerEvent) {
    const deltaPx = startX - ev.clientX
    const deltaPct = (deltaPx / containerWidth) * 100
    cartPct.value = Math.min(CART_MAX_PCT, Math.max(CART_MIN_PCT, Math.round(startPct + deltaPct)))
  }
  function onUp() {
    localStorage.setItem('pos_cart_pct', String(cartPct.value))
    target.removeEventListener('pointermove', onMove)
    target.removeEventListener('pointerup', onUp)
  }
  target.addEventListener('pointermove', onMove)
  target.addEventListener('pointerup', onUp)
}

const loading = ref(true)

// Keyboard shortcuts
useKeyboardShortcuts({
  onHoldOrder: () => holdOrder(),
  onPay: async () => {
    if (cartStore.items.length > 0 && customerStore.customer) {
      await cartStore.flushTaxCalculation()
      if (cartStore.error) return
      paymentStore.openPaymentDialog()
    }
  },
  onCloseDialog: () => {
    if (showReceipt.value) showReceipt.value = false
    else if (paymentStore.showPaymentDialog) paymentStore.closePaymentDialog()
    else if (showHeldOrders.value) showHeldOrders.value = false
    else if (showReturnDialog.value) showReturnDialog.value = false
  },
  onOpenOrders: () => router.push({ name: 'Orders' }),
  onNewOrder: () => startNewOrder(),
  onFocusSearch: () => {
    const searchInput = document.querySelector('[aria-label="Search items"]') as HTMLInputElement
    searchInput?.focus()
  },
  onToggleHeldOrders: () => { showHeldOrders.value = !showHeldOrders.value },
  onToggleReturn: () => { showReturnDialog.value = !showReturnDialog.value },
})

// Company info for display
const companyLogo = ref<string | null>(null)

async function sendInitToDisplay() {
  // Fetch company logo via branding endpoint (no Company doctype permission needed)
  let logo: string | null = null
  if (sessionStore.company) {
    try {
      const branding = await call('pos_prime.api.pos_session.get_branding', {
        company: sessionStore.company,
      })
      if (branding?.company_logo) logo = branding.company_logo
    } catch { /* ignore */ }
  }
  companyLogo.value = logo
  sendDisplayUpdate({
    type: 'init',
    payload: {
      companyName: sessionStore.company || '',
      companyLogo: logo,
      currency: settingsStore.currency || 'USD',
    },
  })
}

onMounted(async () => {
  // Listen for messages from display immediately — must be before any awaits
  // so the handler is registered even if initialization partially fails
  onDisplayMessage(async (message: DisplayMessage) => {
    if (message.type === 'customer_mobile') {
      const mobile = message.payload.mobile
      try {
        const results = await customerStore.searchCustomers(mobile, sessionStore.posProfile)
        if (results && results.length > 0) {
          await customerStore.setCustomer(results[0].name)
          sendDisplayUpdate({
            type: 'customer_result',
            payload: { found: true, customerName: results[0].customer_name || results[0].name },
          })
          // Send cart_update so pole display transitions to cart view with customer name
          const currency = settingsStore.currency || 'USD'
          sendDisplayUpdate({
            type: 'cart_update',
            payload: {
              items: cartStore.items.map((item) => ({
                item_name: item.item_name,
                qty: item.qty,
                rate: item.rate,
                amount: item.amount,
                is_free_item: item.is_free_item || false,
                pricing_rules: item.pricing_rules || null,
                price_list_rate: item.price_list_rate ?? null,
                discount_percentage: item.discount_percentage || 0,
                discount_amount: item.discount_amount || 0,
              })),
              subtotal: cartStore.subtotal,
              netTotal: cartStore.netTotal,
              taxAmount: cartStore.taxAmount,
              grandTotal: cartStore.grandTotal,
              roundedTotal: cartStore.roundedTotal,
              totalItems: cartStore.totalItems,
              currency,
              customerName: results[0].customer_name || results[0].name,
              companyName: sessionStore.company || null,
              discountValue: cartStore.discountValue,
              discountType: cartStore.discountType,
              pricingRuleDiscount: cartStore.pricingRuleDiscount,
            },
          })
        } else {
          sendDisplayUpdate({
            type: 'customer_result',
            payload: { found: false, customerName: null },
          })
        }
      } catch {
        sendDisplayUpdate({
          type: 'customer_result',
          payload: { found: false, customerName: null },
        })
      }
    }
  })

  // Router guard already verified the shift is open and loaded the POS Profile.
  // Only initialize POS-specific state here.
  try {
    // Set default customer from POS Profile
    if (settingsStore.posProfile?.customer && !customerStore.customer) {
      await customerStore.setCustomer(settingsStore.posProfile.customer)
    }
    sendDisplayUpdate({ type: 'idle' })
    // Send company info to display
    sendInitToDisplay()
  } catch (e) {
    console.error('POS initialization error:', e)
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  closeDisplay()
})

// Watch for invoice completion to show receipt
watch(
  () => paymentStore.lastInvoice,
  (invoice) => {
    if (invoice) {
      showReceipt.value = true
      // Notify customer display
      const currency = settingsStore.currency || 'USD'
      sendDisplayUpdate({
        type: 'payment_complete',
        payload: { grandTotal: invoice.rounded_total || invoice.grand_total || 0, currency },
      })
      if (serialDisplay.isConnected.value) {
        serialDisplay.sendToVFD('  Thank You!', `Total: ${(invoice.rounded_total || invoice.grand_total || 0).toFixed(2)}`)
      }
      setTimeout(() => {
        sendDisplayUpdate({ type: 'idle' })
        serialDisplay.clearDisplay()
      }, 5000)
    }
  }
)

// Broadcast cart updates to customer display
watch(
  [
    () => cartStore.items,
    () => cartStore.grandTotal,
    () => cartStore.roundedTotal,
    () => cartStore.taxAmount,
    () => cartStore.discountValue,
    () => customerStore.customer?.customer_name,
  ],
  () => {
    if (cartStore.items.length === 0) {
      sendDisplayUpdate({ type: 'idle' })
      serialDisplay.clearDisplay()
      return
    }
    const currency = settingsStore.currency || 'USD'
    sendDisplayUpdate({
      type: 'cart_update',
      payload: {
        items: cartStore.items.map((item) => ({
          item_name: item.item_name,
          qty: item.qty,
          rate: item.rate,
          amount: item.amount,
          is_free_item: item.is_free_item || false,
          pricing_rules: item.pricing_rules || null,
          price_list_rate: item.price_list_rate ?? null,
          discount_percentage: item.discount_percentage || 0,
          discount_amount: item.discount_amount || 0,
        })),
        subtotal: cartStore.subtotal,
        netTotal: cartStore.netTotal,
        taxAmount: cartStore.taxAmount,
        grandTotal: cartStore.grandTotal,
        roundedTotal: cartStore.roundedTotal,
        totalItems: cartStore.totalItems,
        currency,
        customerName: customerStore.customer?.customer_name || null,
        companyName: sessionStore.company || null,
        discountValue: cartStore.discountValue,
        discountType: cartStore.discountType,
        pricingRuleDiscount: cartStore.pricingRuleDiscount,
      },
    })
    // VFD: show last added item + total
    if (serialDisplay.isConnected.value) {
      const lastItem = cartStore.items[cartStore.items.length - 1]
      const total = (cartStore.roundedTotal || cartStore.grandTotal).toFixed(2)
      serialDisplay.sendToVFD(
        `${lastItem.item_name.substring(0, 14)} x${lastItem.qty}`,
        `TOTAL: ${total.padStart(13)}`
      )
    }
  },
  { deep: true }
)

// Helper to build payment display payload
function buildPaymentPayload() {
  const grandTotal = cartStore.roundedTotal || cartStore.grandTotal
  return {
    grandTotal,
    currency: settingsStore.currency || 'USD',
    payments: paymentStore.payments.filter(p => p.amount > 0),
    customerName: customerStore.customer?.customer_name || null,
    totalPaid: paymentStore.totalPaid,
    changeDue: paymentStore.changeAmount(grandTotal),
  }
}

// Broadcast payment start
watch(
  () => paymentStore.showPaymentDialog,
  (show) => {
    if (show) {
      sendDisplayUpdate({ type: 'payment_start', payload: buildPaymentPayload() })
      if (serialDisplay.isConnected.value) {
        serialDisplay.sendToVFD('Processing...', `Total: ${(cartStore.roundedTotal || cartStore.grandTotal).toFixed(2).padStart(13)}`)
      }
    }
  }
)

// Live-update pole display as cashier changes payment amounts
watch(
  () => paymentStore.payments.map(p => p.amount),
  () => {
    if (paymentStore.showPaymentDialog) {
      sendDisplayUpdate({ type: 'payment_start', payload: buildPaymentPayload() })
    }
  },
  { deep: true }
)

async function startNewOrder() {
  cartStore.$reset()
  paymentStore.$reset()
  draftsStore.clearActiveDraft()
  showReceipt.value = false
  sendDisplayUpdate({ type: 'idle' })
  serialDisplay.clearDisplay()
  // Reset to default customer
  customerStore.$reset()
  if (settingsStore.posProfile?.customer) {
    await customerStore.setCustomer(settingsStore.posProfile.customer)
  }
  // Refresh items to get updated stock quantities
  itemsStore.fetchAllItems()
}

async function holdOrder() {
  if (cartStore.items.length === 0) return
  if (!customerStore.customer) return

  try {
    const opts = cartStore.invoiceOptions || {}
    await draftsStore.saveDraft({
      customer: customerStore.customer.name,
      pos_profile: sessionStore.posProfile,
      payments: paymentStore.payments.filter((payment) => payment.amount > 0),
      items: cartStore.items.filter((i) => !i.is_free_item).map((item) => ({
        item_code: item.item_code,
        qty: item.qty,
        rate: item.pricing_rules ? (item.price_list_rate ?? item.rate) : item.rate,
        price_list_rate: item.price_list_rate ?? item.rate,
        discount_percentage: item.pricing_rules ? 0 : item.discount_percentage,
        discount_amount: item.pricing_rules ? undefined : (item.discount_amount || undefined),
        serial_no: item.serial_no || undefined,
        batch_no: item.batch_no || undefined,
        serial_and_batch_bundle: item.serial_and_batch_bundle || undefined,
        uom: item.uom || undefined,
        conversion_factor: item.conversion_factor || 1,
        item_tax_template: item.item_tax_template || undefined,
        margin_type: item.margin_type || undefined,
        margin_rate_or_amount: item.margin_rate_or_amount || undefined,
        description: item.description || undefined,
        project: item.project || undefined,
        weight_per_unit: item.weight_per_unit || undefined,
        weight_uom: item.weight_uom || undefined,
      })),
      additional_discount_percentage: cartStore.pricingRuleDiscount?.type === 'percentage'
        ? cartStore.pricingRuleDiscount.value
        : cartStore.additionalDiscountPercentage,
      discount_amount: cartStore.pricingRuleDiscount?.type === 'amount'
        ? cartStore.pricingRuleDiscount.value
        : cartStore.additionalDiscountAmount,
      apply_discount_on: cartStore.applyDiscountOn,
      coupon_code: cartStore.couponCode || undefined,
      ...Object.fromEntries(
        Object.entries(opts).filter(([_, value]) => value != null && value !== '' && value !== false)
      ),
    })
    startNewOrder()
  } catch {
    // Show error via toast or similar
  }
}

async function resumeDraft(invoiceName: string) {
  showHeldOrders.value = false
  try {
    const draft = await draftsStore.loadDraft(invoiceName)
    if (!draft) return

    // Reset cart and populate from draft
    cartStore.$reset()
    paymentStore.$reset()

    // Set customer
    if (draft.customer) {
      await customerStore.setCustomer(draft.customer)
    }

    if (itemsStore.allItems.length === 0) {
      await itemsStore.fetchAllItems(sessionStore.posProfile)
    }

    // Add items to cart
    for (const item of draft.items || []) {
      const catalogItem = itemsStore.allItems.find((candidate) => candidate.item_code === item.item_code)
      cartStore.items.push({
        item_code: item.item_code,
        item_name: item.item_name,
        rate: item.pricing_rules
          ? item.rate
          : (item.price_list_rate ?? item.rate),
        qty: item.qty,
        amount: item.amount ?? item.rate * item.qty,
        uom: item.uom || '',
        discount_percentage: item.discount_percentage || 0,
        discount_amount: item.discount_amount || 0,
        image: null,
        stock_uom: item.stock_uom || item.uom || '',
        has_serial_no: !!item.serial_no,
        has_batch_no: !!item.batch_no,
        serial_no: item.serial_no || null,
        batch_no: item.batch_no || null,
        serial_and_batch_bundle: item.serial_and_batch_bundle || null,
        conversion_factor: item.conversion_factor || 1,
        item_tax_template: item.item_tax_template || null,
        margin_type: item.margin_type || null,
        margin_rate_or_amount: item.margin_rate_or_amount || 0,
        description: item.description || null,
        project: item.project || null,
        weight_per_unit: item.weight_per_unit || null,
        weight_uom: item.weight_uom || null,
        actual_qty: catalogItem?.actual_qty ?? item.actual_qty,
        is_stock_item: catalogItem?.is_stock_item ?? item.is_stock_item,
        pricing_rules: item.pricing_rules || null,
        price_list_rate: item.price_list_rate ?? null,
      })
    }

    // Restore invoice-level options from draft
    cartStore.setInvoiceOptions({
      customer_address: draft.customer_address || null,
      shipping_address_name: draft.shipping_address_name || null,
      contact_person: draft.contact_person || null,
      conversion_rate: draft.conversion_rate || null,
      price_list_currency: draft.price_list_currency || null,
      plc_conversion_rate: draft.plc_conversion_rate || null,
      sales_partner: draft.sales_partner || null,
      commission_rate: draft.commission_rate || null,
      project: draft.project || null,
      cost_center: draft.cost_center || null,
      remarks: draft.remarks || null,
      po_no: draft.po_no || null,
      po_date: draft.po_date || null,
      set_posting_time: !!draft.set_posting_time,
      posting_date: draft.posting_date || null,
      posting_time: draft.posting_time || null,
      naming_series: draft.naming_series || null,
      shipping_rule: draft.shipping_rule || null,
      tc_name: draft.tc_name || null,
      terms: draft.terms || null,
      letter_head: draft.letter_head || null,
      select_print_heading: draft.select_print_heading || null,
      group_same_items: !!draft.group_same_items,
      language: draft.language || null,
      payment_terms_template: draft.payment_terms_template || null,
      allocate_advances_automatically: !!draft.allocate_advances_automatically,
      debit_to: draft.debit_to || null,
      sales_team: draft.sales_team || [],
    })

    paymentStore.payments = (draft.payments || []).map((payment: any) => ({
      mode_of_payment: payment.mode_of_payment,
      amount: Number(payment.amount) || 0,
    }))
    paymentStore.activePaymentMethod =
      paymentStore.payments.find((payment) => payment.amount > 0)?.mode_of_payment
      || paymentStore.payments[0]?.mode_of_payment
      || ''

    // Restore the target before the value; it changes ERPNext's tax allocation.
    cartStore.setApplyDiscountOn(
      draft.apply_discount_on === 'Net Total' ? 'Net Total' : 'Grand Total'
    )

    // Restore discount
    if (draft.additional_discount_percentage) {
      cartStore.setDiscount('percentage', draft.additional_discount_percentage)
    } else if (draft.discount_amount) {
      cartStore.setDiscount('amount', draft.discount_amount)
    }
    if (draft.coupon_code) {
      cartStore.setCouponCode(draft.coupon_code)
    }

    // Trigger tax recalculation
    cartStore.calculateTaxes()
  } catch {
    // Handle error
  }
}

function onReturnCompleted() {
  showReturnDialog.value = false
  startNewOrder()
}
</script>

<template>
  <div v-if="loading" :class="['flex items-center justify-center bg-gray-50 dark:bg-gray-900', isDeskMode ? 'h-full' : 'h-screen']">
    <div class="text-gray-400 dark:text-gray-500 text-sm">{{ __('Loading POS...') }}</div>
  </div>

  <AppShell v-else @toggle-held-orders="showHeldOrders = !showHeldOrders" @toggle-return="showReturnDialog = true">
    <!-- ERPNext-style layout with resizable panels -->
    <div class="pos-layout flex h-full overflow-hidden" style="gap: var(--margin-md, 8px); padding: 0.5%;">
      <!-- Items panel -->
      <div
        class="flex flex-col overflow-hidden pos-card rounded-lg min-w-0"
        :class="{ 'hidden sm:flex': mobileTab === 'cart' }"
        :style="{ flex: `${100 - cartPct} 1 0%` }"
      >
        <ItemGrid />
      </div>

      <!-- Resize handle -->
      <div
        class="hidden sm:flex items-center justify-center cursor-col-resize shrink-0 group select-none"
        style="width: 6px;"
        @pointerdown.prevent="onResizeStart"
      >
        <div class="w-1 h-8 rounded-full bg-gray-200 dark:bg-gray-700 group-hover:bg-blue-400 group-active:bg-blue-500 transition-colors" />
      </div>

      <!-- Cart panel -->
      <div
        class="flex flex-col overflow-hidden pos-card rounded-lg min-w-0"
        :class="{ 'hidden sm:flex': mobileTab === 'items' }"
        :style="{ flex: `${cartPct} 1 0%` }"
      >
        <Cart @hold-order="holdOrder" />
      </div>
    </div>

    <!-- Mobile tab switcher -->
    <div class="sm:hidden fixed bottom-14 left-0 right-0 flex bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-800 z-10">
      <button
        @click="mobileTab = 'items'"
        class="flex-1 flex items-center justify-center gap-2 py-2.5 text-sm font-medium transition-colors"
        :class="mobileTab === 'items' ? 'text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/30' : 'text-gray-500 dark:text-gray-400'"
      >
        <LayoutGrid :size="16" />
        {{ __('Items') }}
      </button>
      <button
        @click="mobileTab = 'cart'"
        class="flex-1 flex items-center justify-center gap-2 py-2.5 text-sm font-medium transition-colors relative"
        :class="mobileTab === 'cart' ? 'text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/30' : 'text-gray-500 dark:text-gray-400'"
      >
        <ShoppingCart :size="16" />
        {{ __('Cart') }}
        <span
          v-if="cartStore.totalItems > 0"
          class="absolute top-1 right-1/4 bg-blue-600 text-white text-[10px] font-bold rounded-full w-4 h-4 flex items-center justify-center"
        >
          {{ cartStore.totalItems > 9 ? '9+' : cartStore.totalItems }}
        </span>
      </button>
    </div>

    <!-- Payment dialog -->
    <PaymentDialog v-if="paymentStore.showPaymentDialog" />

    <!-- Receipt preview -->
    <ReceiptPreview
      v-if="showReceipt"
      @new-order="startNewOrder"
      @close="startNewOrder"
    />

    <!-- Held orders drawer -->
    <HeldOrdersDrawer
      v-if="showHeldOrders"
      @close="showHeldOrders = false"
      @resume="resumeDraft"
    />

    <!-- Manual return dialog -->
    <ReturnSearchDialog
      v-if="showReturnDialog"
      @close="showReturnDialog = false"
      @completed="onReturnCompleted"
    />
  </AppShell>
</template>

<style scoped>
/* Mobile: stack panels */
@media screen and (max-width: 640px) {
  .pos-layout {
    flex-direction: column;
    padding: 4px !important;
  }
  .pos-layout > div {
    flex: 1 1 auto !important;
  }
}
</style>
