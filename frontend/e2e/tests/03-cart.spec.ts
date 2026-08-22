import { test, expect } from '../fixtures/pos-fixtures'
import { POSPage } from '../fixtures/pos-page'

function currencyValue(value: string): number {
  return Number(value.replace(/[^0-9.-]/g, ''))
}

test.describe('Cart Operations', () => {
  let pos: POSPage

  test.beforeEach(async ({ posPage, testData }) => {
    pos = new POSPage(posPage)
    await pos.waitForPOSReady()

    if (await pos.hasOpeningEntry()) {
      await pos.fillOpeningEntry(testData.posProfile, { Cash: 0 })
      await posPage.waitForTimeout(2000)
    }
    await posPage.waitForTimeout(2000)
  })

  test('should add item to cart by clicking', async ({ posPage, testData }) => {
    await pos.searchItem(testData.items.stock)
    await posPage.waitForTimeout(1000)

    if (await pos.hasItemCards()) {
      await pos.clickFirstItemCard()

      const cartCount = await pos.getCartItemCount()
      expect(cartCount).toBeGreaterThanOrEqual(1)
    }
  })

  test('should increment quantity when adding same item twice', async ({ posPage, testData }) => {
    // Search for item
    await pos.searchItem(testData.items.stock)
    await posPage.waitForTimeout(1500)

    // Click the item card once
    if (await pos.hasItemCards()) {
      await pos.clickFirstItemCard()
      await posPage.waitForTimeout(2000) // wait for cart + tax calc to fully settle

      // Click the same item card again (search results still visible)
      await pos.clickFirstItemCard()
      await posPage.waitForTimeout(2000)
    }

    // Cart should still have 1 item (merged) with qty 2
    const cartCount = await pos.getCartItemCount()
    expect(cartCount).toBe(1)

    const qty = await pos.getCartItemQty(0)
    expect(qty).toBe(2)
  })

  test('should increase quantity with + button', async ({ posPage, testData }) => {
    await pos.addItemBySearch(testData.items.stock)

    await pos.increaseQty(0)
    await posPage.waitForTimeout(300)

    const qty = await pos.getCartItemQty(0)
    expect(qty).toBe(2)
  })

  test('should decrease quantity with - button', async ({ posPage, testData }) => {
    await pos.addItemBySearch(testData.items.stock)

    // Increase qty to 3
    await pos.increaseQty(0)
    await pos.increaseQty(0)
    await posPage.waitForTimeout(300)

    // Decrease qty
    await pos.decreaseQty(0)
    await posPage.waitForTimeout(300)

    const qty = await pos.getCartItemQty(0)
    expect(qty).toBe(2)
  })

  test('should remove item when qty reaches 0', async ({ posPage, testData }) => {
    await pos.addItemBySearch(testData.items.stock)

    // Decrease to 0
    await pos.decreaseQty(0)
    await posPage.waitForTimeout(500)

    const cartCount = await pos.getCartItemCount()
    expect(cartCount).toBe(0)
  })

  test('should remove item with delete button', async ({ posPage, testData }) => {
    await pos.addItemBySearch(testData.items.stock)

    await pos.removeItem(0)
    await posPage.waitForTimeout(500)

    const cartCount = await pos.getCartItemCount()
    expect(cartCount).toBe(0)
  })

  test('should add multiple different items to cart', async ({ posPage, testData }) => {
    // Skip if stock and nonStock are the same item (fallback when no non-stock item exists)
    test.skip(testData.items.stock === testData.items.nonStock, 'No distinct non-stock item available')

    await pos.addItemBySearch(testData.items.stock)
    await pos.addItemBySearch(testData.items.nonStock)

    const cartCount = await pos.getCartItemCount()
    expect(cartCount).toBe(2)
  })

  test('should update cart totals when items are added', async ({ posPage, testData }) => {
    await pos.addItemBySearch(testData.items.stock)
    await pos.waitForTaxCalculation()

    const total = await pos.getCartTotal()
    // Total should be non-zero
    expect(total).not.toBe('0')
    expect(total).not.toBe('0.00')
  })

  test('should display Total Qty correctly', async ({ posPage, testData }) => {
    await pos.addItemBySearch(testData.items.stock)
    await pos.increaseQty(0) // qty = 2
    await pos.waitForTaxCalculation()

    const qty = await pos.getTotalQty()
    expect(qty).toContain('2')
  })

  test('should keep item percentage discount totals in sync', async ({ testData }) => {
    await pos.addItemBySearch(testData.items.nonStock)
    await pos.increaseQty(0)
    await pos.waitForTaxCalculation()

    const originalNet = currencyValue(await pos.getNetTotal())
    await pos.applyItemDiscount(0, 'percentage', 10)
    const expected = originalNet * 0.9

    expect(currencyValue(await pos.getNetTotal())).toBeCloseTo(expected, 2)
    expect(currencyValue(await pos.getCartTotal())).toBeCloseTo(expected, 2)
    expect(currencyValue(await pos.getCheckoutTotal())).toBeCloseTo(expected, 2)
  })

  test('should apply flat item discount per unit and clear it when switching modes', async ({ testData }) => {
    await pos.addItemBySearch(testData.items.nonStock)
    await pos.increaseQty(0)
    await pos.waitForTaxCalculation()

    const originalNet = currencyValue(await pos.getNetTotal())
    await pos.applyItemDiscount(0, 'amount', 15)
    const flatExpected = originalNet - 30
    expect(currencyValue(await pos.getNetTotal())).toBeCloseTo(flatExpected, 2)
    expect(currencyValue(await pos.getCartTotal())).toBeCloseTo(flatExpected, 2)
    expect(currencyValue(await pos.getCheckoutTotal())).toBeCloseTo(flatExpected, 2)

    await pos.applyItemDiscount(0, 'percentage', 10)
    const percentExpected = originalNet * 0.9
    expect(currencyValue(await pos.getNetTotal())).toBeCloseTo(percentExpected, 2)
    expect(currencyValue(await pos.getCartTotal())).toBeCloseTo(percentExpected, 2)
    expect(currencyValue(await pos.getCheckoutTotal())).toBeCloseTo(percentExpected, 2)
  })

  test('should show zero in every payable total after a full flat item discount', async ({ testData }) => {
    await pos.addItemBySearch(testData.items.nonStock)
    await pos.waitForTaxCalculation()

    const unitPrice = currencyValue(await pos.getNetTotal())
    expect(unitPrice).toBeGreaterThan(0)

    await pos.applyItemDiscount(0, 'amount', unitPrice)

    expect(currencyValue(await pos.getNetTotal())).toBeCloseTo(0, 2)
    expect(currencyValue(await pos.getCartTotal())).toBeCloseTo(0, 2)
    expect(currencyValue(await pos.getCheckoutTotal())).toBeCloseTo(0, 2)
  })
})
