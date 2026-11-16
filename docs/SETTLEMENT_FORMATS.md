# Settlement CSV formats

ReturnProof imports marketplace settlement/payment exports as CSV and stores
money as integer paise. All sample files under `samples/settlements/` are
synthetic and contain invented order numbers.

## Canonical columns

| Column | Required | Meaning |
|---|---|---|
| `order_number` | yes | Marketplace order ID |
| `transaction_type` | yes | `sale` / `order_payment`, `refund`, `claim_payout`, `return` |
| `marketplace_sku` | no | Seller SKU on the settlement line |
| `quantity` | no | Units (default 1) |
| `gross_inr` | one monetary column | Gross sale or payout value |
| `commission_inr` | no | Marketplace commission / referral fee |
| `shipping_inr` | no | Forward shipping fee |
| `reverse_shipping_inr` | no | Return / reverse pickup fee |
| `refund_inr` | no | Customer refund |
| `other_fee_inr` | no | Any other deduction |
| `net_inr` | no | Actual settled amount; derived from the other columns when absent |
| `claim_payout_inr` | no | Reimbursement amount on a `claim_payout` row |

Amounts are INR decimals (`1499.00`, `₹1,499.00`, `(89.00)` for negatives).
Header matching is case-insensitive and ignores spaces, hyphens and `&`.

## Header aliases

| Canonical | Accepted aliases |
|---|---|
| `order_number` | `order_id`, `order`, `order-id`, `amazon_order_id` |
| `transaction_type` | `type`, `transaction`, `transaction-type` |
| `gross_inr` | `gross`, `product_sales`, `product-sales`, `sales`, `item_price` |
| `commission_inr` | `commission`, `referral_fee`, `marketplace_fee` |
| `shipping_inr` | `shipping`, `shipping_fee`, `shipping-fee`, `delivery_fee` |
| `reverse_shipping_inr` | `reverse_shipping`, `reverse-shipping`, `return_shipping` |
| `refund_inr` | `refund`, `refund_amount`, `refund-amount`, `customer_refund` |
| `other_fee_inr` | `other_fee`, `other_fees`, `other-fees` |
| `net_inr` | `net`, `net_amount`, `net-amount`, `settlement_amount`, `payout` |
| `claim_payout_inr` | `claim_payout`, `reimbursement`, `reimbursement_amount` |

The Amazon MTR and Flipkart payment samples demonstrate alias-based imports;
there is no marketplace API integration and no network call.

## Leakage rules

1. **Refund without return** — a refund row whose order has no return request,
   or whose return has not been received, creates an at-risk entry.
2. **Reverse shipping error** — a reverse-shipping charge on an order with no
   return, an unreceived return, or a seller-protected reason (damaged, wrong
   item, empty box, not as described).
3. **Commission mismatch** — commission above the marketplace's configured
   `commission_percent` (default 12%) plus tolerance.
4. **Fee mismatch** — settlement net below `gross - commission - shipping -
   reverse shipping - refund - other fees` plus tolerance.
5. **Approved claim not paid** — an approved/partial claim whose approved
   amount is not covered by `claim_payout` settlement rows.
6. **Claim payout** — a matching payout marks the approved-claim ledger entry
   as recovered.

Tolerances are configurable through `RETURNPROOF_SETTLEMENT_TOLERANCE_PAISE`
(default 100 paise) and `RETURNPROOF_DEFAULT_COMMISSION_PERCENT` (default 12).
