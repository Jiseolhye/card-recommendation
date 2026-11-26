const amount = document.querySelector('#payment-form input[name="amount"]');
const preview = document.querySelector('#amount-preview');
const quickAmounts = document.querySelector('#quick-amounts');
const amountButtons = quickAmounts.querySelectorAll('button[data-amount]');
const wonFormat = new Intl.NumberFormat('ko-KR');

function updateAmountPreview() {
    const validAmount = /^[0-9]+$/.test(amount.value) && amount.value.length <= amount.maxLength;
    preview.hidden = !validAmount;
    if (validAmount) {
        preview.textContent = `입력한 금액 ${wonFormat.format(BigInt(amount.value))}원`;
    }
    for (const button of amountButtons) {
        button.setAttribute('aria-pressed', String(button.dataset.amount === amount.value));
    }
}

quickAmounts.hidden = false;
amount.addEventListener('input', updateAmountPreview);
for (const button of amountButtons) {
    button.addEventListener('click', () => {
        amount.value = button.dataset.amount;
        updateAmountPreview();
    });
}
updateAmountPreview();
