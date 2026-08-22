<!-- Copyright (c) 2026, Ravindu Gajanayaka -->
<!-- Licensed under GPLv3. See license.txt -->

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useSettingsStore } from "@/stores/settings";
import { useCurrency } from "@/composables/useCurrency";
import { call } from "frappe-ui";
import { X, RotateCcw } from "lucide-vue-next";

const props = defineProps<{
	order: Record<string, any>;
}>();

const emit = defineEmits<{
	close: [];
	completed: [data: any];
}>();

const settingsStore = useSettingsStore();
const { formatCurrency } = useCurrency();

interface ReturnItem {
	source_row_name: string;
	item_code: string;
	item_name: string;
	max_qty: number;
	return_qty: number;
	rate: number;
	conversion_factor: number;
	has_serial_no: boolean;
	has_batch_no: boolean;
	serial_no: string;
	batch_no: string;
	serial_and_batch_bundle: string;
	use_serial_batch_fields: boolean;
}

const returnItems = ref<ReturnItem[]>(
	(props.order?.items || []).map((item: any) => ({
		source_row_name: item.source_row_name || item.name,
		item_code: item.item_code,
		item_name: item.item_name,
		max_qty: Math.abs(Number(item.qty) || 0),
		return_qty: Math.abs(Number(item.qty) || 0),
		rate: Number(item.rate) || 0,
		conversion_factor: Number(item.conversion_factor) || 1,
		has_serial_no: Boolean(item.has_serial_no),
		has_batch_no: Boolean(item.has_batch_no),
		serial_no: item.serial_no || "",
		batch_no: item.batch_no || "",
		serial_and_batch_bundle: item.serial_and_batch_bundle || "",
		use_serial_batch_fields: Boolean(item.use_serial_batch_fields),
	})),
);

const returnPaymentMethods = computed(() =>
	settingsStore.paymentMethods.filter((payment) => payment.allow_in_returns),
);
const selectedPaymentMethod = ref(returnPaymentMethods.value[0]?.mode_of_payment || "");

watch(returnPaymentMethods, (methods) => {
	if (!methods.some((method) => method.mode_of_payment === selectedPaymentMethod.value)) {
		selectedPaymentMethod.value = methods[0]?.mode_of_payment || "";
	}
});

const returnTotal = computed(() =>
	returnItems.value.reduce((sum, item) => sum + item.return_qty * item.rate, 0),
);

const loading = ref(false);
const error = ref("");
const returnInvoiceName = ref("");
const completed = ref(false);

function serialNumbers(value: string) {
	return value
		.replace(/,/g, "\n")
		.split(/\r?\n/)
		.map((serialNo) => serialNo.trim())
		.filter(Boolean);
}

function isBundleOnly(item: ReturnItem) {
	return Boolean(item.serial_and_batch_bundle && !item.use_serial_batch_fields);
}

async function closeDialog() {
	if (loading.value) return;

	error.value = "";
	if (returnInvoiceName.value && !completed.value) {
		loading.value = true;
		try {
			await call("pos_prime.api.orders.discard_return_invoice", {
				invoice_name: returnInvoiceName.value,
			});
			returnInvoiceName.value = "";
		} catch (e: any) {
			error.value = e.messages?.[0] || e.message || __("Failed to discard return draft");
			return;
		} finally {
			loading.value = false;
		}
	}

	emit("close");
}

async function processReturn() {
	error.value = "";

	// Validate at least one item has qty > 0
	const hasReturnItems = returnItems.value.some((item) => item.return_qty > 0);
	if (!hasReturnItems) {
		error.value = __("Please select at least one item to return");
		return;
	}
	if (!selectedPaymentMethod.value) {
		error.value = __("No refund method is enabled for returns in this POS Profile");
		return;
	}
	// Validate quantities don't exceed max
	for (const item of returnItems.value) {
		if (!item.source_row_name) {
			error.value = __("Reload this order before processing its return");
			return;
		}
		if (item.return_qty > item.max_qty) {
			error.value = `Return qty for ${item.item_name} cannot exceed ${item.max_qty}`;
			return;
		}
		if (item.return_qty < 0) {
			error.value = `Return qty for ${item.item_name} cannot be negative`;
			return;
		}
		if (isBundleOnly(item) && item.return_qty !== 0 && item.return_qty !== item.max_qty) {
			error.value = `${item.item_name} must be returned as a full invoice row`;
			return;
		}
		if (item.serial_no && item.return_qty > 0) {
			const serialCount = item.return_qty * item.conversion_factor;
			if (
				!Number.isInteger(serialCount) ||
				serialNumbers(item.serial_no).length !== serialCount
			) {
				error.value = `Select ${serialCount} serial number(s) for ${item.item_name}`;
				return;
			}
		}
	}

	loading.value = true;
	try {
		if (!returnInvoiceName.value) {
			const returnDoc = await call("pos_prime.api.orders.create_return_invoice", {
				source_invoice: props.order.name,
			});
			if (!returnDoc?.name) {
				throw new Error(__("The return draft could not be created"));
			}
			returnInvoiceName.value = returnDoc.name;
		}

		// Submit with adjusted quantities
		const result = await call("pos_prime.api.orders.submit_return_invoice", {
			invoice_name: returnInvoiceName.value,
			items: returnItems.value.map((item) => ({
				source_row_name: item.source_row_name,
				qty: item.return_qty > 0 ? -item.return_qty : 0,
				serial_no: item.serial_no || undefined,
				batch_no: item.batch_no || undefined,
			})),
			payments: [
				{
					mode_of_payment: selectedPaymentMethod.value,
				},
			],
		});

		completed.value = true;
		returnInvoiceName.value = "";
		emit("completed", result);
	} catch (e: any) {
		error.value = e.messages?.[0] || e.message || "Failed to process return";
	} finally {
		loading.value = false;
	}
}
</script>

<template>
	<div
		class="fixed inset-0 z-50 flex items-center justify-center p-4"
		role="dialog"
		aria-modal="true"
		:aria-label="__('Process Return')"
		@keydown.escape="closeDialog"
	>
		<div class="absolute inset-0 bg-black/30 dark:bg-black/50" @click="closeDialog" />
		<div
			class="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl dark:shadow-black/30 w-full max-w-lg max-h-[90vh] overflow-y-auto"
		>
			<div
				class="sticky top-0 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 px-5 py-3 flex items-center justify-between rounded-t-xl z-10"
			>
				<div class="flex items-center gap-2">
					<RotateCcw :size="18" class="text-red-600 dark:text-red-400" />
					<h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">
						{{ __("Process Return") }}
					</h3>
				</div>
				<button
					@click="closeDialog"
					:disabled="loading"
					class="text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 disabled:opacity-50"
				>
					<X :size="18" />
				</button>
			</div>

			<div class="p-5 space-y-4">
				<div
					v-if="error"
					class="p-3 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-400 rounded-lg text-sm"
				>
					{{ error }}
				</div>

				<div class="text-sm text-gray-500 dark:text-gray-400">
					{{ __("Returning against") }}:
					<span class="font-medium text-gray-700 dark:text-gray-300">{{
						order.name
					}}</span>
				</div>

				<!-- Return items -->
				<div class="space-y-2">
					<div
						v-for="(item, index) in returnItems"
						:key="item.source_row_name"
						class="bg-gray-50 dark:bg-gray-800 rounded-lg p-3"
					>
						<div class="flex items-center gap-3">
							<div class="flex-1 min-w-0">
								<div class="text-sm font-medium text-gray-800 dark:text-gray-200">
									{{ item.item_name }}
								</div>
								<div class="text-xs text-gray-500 dark:text-gray-400">
									{{ formatCurrency(item.rate) }} &middot; {{ __("Max") }}:
									{{ item.max_qty }}
								</div>
								<div
									v-if="item.batch_no"
									class="text-xs text-gray-500 dark:text-gray-400"
								>
									{{ __("Batch") }}: {{ item.batch_no }}
								</div>
							</div>
							<div class="flex items-center gap-2">
								<label class="text-xs text-gray-500 dark:text-gray-400"
									>{{ __("Qty") }}:</label
								>
								<input
									v-model.number="returnItems[index].return_qty"
									type="number"
									:min="0"
									:max="item.max_qty"
									class="w-16 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 px-2 py-1 text-sm text-center focus:outline-none focus:ring-2 focus:ring-red-500"
								/>
							</div>
							<div
								class="w-20 text-right text-sm font-medium text-gray-800 dark:text-gray-200"
							>
								{{ formatCurrency(item.return_qty * item.rate) }}
							</div>
						</div>
						<div
							v-if="isBundleOnly(item)"
							class="mt-2 text-xs text-gray-500 dark:text-gray-400"
						>
							{{ __("Serial/batch bundle rows can only be returned in full") }}
						</div>
						<div v-else-if="item.has_serial_no && item.serial_no" class="mt-2">
							<label class="block text-xs text-gray-500 dark:text-gray-400 mb-1">{{
								__("Serial Numbers")
							}}</label>
							<textarea
								v-model="returnItems[index].serial_no"
								rows="2"
								class="w-full resize-y rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-red-500"
							/>
						</div>
					</div>
				</div>

				<!-- Refund method -->
				<div>
					<label
						class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
						>{{ __("Refund Method") }}</label
					>
					<select
						v-model="selectedPaymentMethod"
						class="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-500"
					>
						<option
							v-for="pm in returnPaymentMethods"
							:key="pm.mode_of_payment"
							:value="pm.mode_of_payment"
						>
							{{ pm.mode_of_payment }}
						</option>
					</select>
				</div>

				<!-- Return total -->
				<div class="bg-red-50 dark:bg-red-900/20 rounded-lg p-4">
					<div
						class="flex justify-between text-sm font-bold text-red-700 dark:text-red-400"
					>
						<span>{{ __("Estimated Refund") }}</span>
						<span>{{ formatCurrency(returnTotal) }}</span>
					</div>
				</div>

				<button
					@click="processReturn"
					:disabled="
						loading ||
						!returnItems.some((item) => item.return_qty > 0) ||
						!selectedPaymentMethod
					"
					class="w-full py-3 bg-red-600 text-white rounded-lg text-sm font-semibold hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
				>
					<RotateCcw :size="16" />
					{{ loading ? __("Processing...") : __("Process Return") }}
				</button>
			</div>
		</div>
	</div>
</template>
