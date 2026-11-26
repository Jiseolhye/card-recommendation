from datetime import date, datetime, time

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .benefit_calculation import SEOUL, month_bounds
from .models import Card, Category, UserCard


LOGIN_ERROR = '아이디 또는 비밀번호를 다시 확인해 주세요.'
# Bound the text length before parsing and apply the web payment cap separately.
MAX_PAYMENT_DIGITS = 23
MAX_WEB_PAYMENT = 100_000_000


class LoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        'invalid_login': LOGIN_ERROR,
        'inactive': LOGIN_ERROR,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = '아이디'
        self.fields['username'].widget.attrs.pop('autofocus', None)
        self.fields['username'].widget.attrs['autocomplete'] = 'username'
        self.fields['password'].label = '비밀번호'


class SignUpForm(UserCreationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = '아이디'
        self.fields['username'].widget.attrs.pop('autofocus', None)
        self.fields['username'].widget.attrs['autocomplete'] = 'username'
        self.fields['password1'].label = '비밀번호'
        self.fields['password2'].label = '비밀번호 확인'

    def clean_username(self):
        username = super().clean_username()
        if username is None:
            return None

        # SQLite's case-insensitive match does not cover all Unicode usernames.
        user_model = self._meta.model
        existing_names = user_model.objects.values_list('username', flat=True).iterator()
        folded_username = username.casefold()
        if any(name.casefold() == folded_username for name in existing_names):
            raise self.instance.unique_error_message(user_model, ['username'])
        return username


class PaymentForm(forms.Form):
    category = forms.ChoiceField(
        label='결제 업종', choices=[('', '업종 선택'), *Category.choices],
        error_messages={'required': '결제 업종을 선택해 주세요.', 'invalid_choice': '목록에서 업종을 선택해 주세요.'},
    )
    amount = forms.RegexField(
        label='결제 예정 금액', regex=r'\A[0-9]+\Z', max_length=MAX_PAYMENT_DIGITS, strip=False,
        help_text='1원부터 1억원까지 입력할 수 있어요.',
        widget=forms.TextInput(attrs={'inputmode': 'numeric', 'placeholder': '예: 50000'}),
        error_messages={
            'required': '결제 예정 금액을 입력해 주세요.',
            'invalid': '결제금액은 원 단위 숫자만 입력해 주세요.',
            'max_length': '결제금액은 %(limit_value)s자리 이내로 입력해 주세요.',
        },
    )

    def clean_amount(self):
        amount = int(self.cleaned_data['amount'])
        if amount == 0:
            raise forms.ValidationError('결제금액은 1원 이상 입력해 주세요.', code='non_positive')
        if amount > MAX_WEB_PAYMENT:
            raise forms.ValidationError('결제 예정 금액은 100,000,000원까지 입력할 수 있어요.', code='max_value')
        return amount


class AddCardForm(forms.Form):
    card = forms.ModelChoiceField(
        label='카드 상품', queryset=Card.objects.none(), empty_label='카드 상품 선택',
        error_messages={'invalid_choice': '이미 등록했거나 존재하지 않는 카드예요.'},
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['card'].queryset = Card.objects.exclude(
            pk__in=user.user_cards.values('card_id'),
        ).order_by('issuer', 'name', 'pk')


class TransactionFilterForm(forms.Form):
    month = forms.RegexField(
        label='조회 월', regex=r'\A[0-9]{4}-(0[1-9]|1[0-2])\Z', max_length=7,
        strip=False, widget=forms.Select,
        error_messages={
            'required': '조회 월을 선택해 주세요.',
            'invalid': '조회 월은 YYYY-MM 형식의 올바른 연월이어야 해요.',
            'max_length': '조회 월은 YYYY-MM 형식으로 입력해 주세요.',
        },
    )
    card = forms.ModelChoiceField(
        label='카드 선택', queryset=UserCard.objects.none(), required=False,
        empty_label='전체 카드', error_messages={'invalid_choice': '보유한 카드 중에서 선택해 주세요.'},
    )

    def __init__(self, *args, current_month, past_months, owned_cards, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_month = current_month
        self.available_months = {current_month, *past_months}
        self._set_month_choices()
        self.fields['card'].queryset = owned_cards
        self.fields['card'].choices = [('', '전체 카드'), *((card.pk, str(card.card)) for card in owned_cards)]

    def _set_month_choices(self):
        choices = [
            (month.isoformat()[:7], f'{month.year}년 {month.month}월')
            for month in sorted(self.available_months, reverse=True)
        ]
        self.fields['month'].widget.choices = [('', '조회 월 선택'), *choices]

    def clean_month(self):
        try:
            month = date.fromisoformat(f"{self.cleaned_data['month']}-01")
        except ValueError:
            raise forms.ValidationError('올바른 조회 월을 입력해 주세요.', code='invalid') from None
        if month > self.current_month:
            raise forms.ValidationError('미래의 이용내역은 조회할 수 없어요.', code='future')
        try:
            bounds = month_bounds(datetime.combine(month, time.min, tzinfo=SEOUL))
        except OverflowError:
            raise forms.ValidationError('조회할 수 있는 연월 범위를 벗어났어요.', code='invalid') from None
        if month not in self.available_months:
            self.available_months.add(month)
            self._set_month_choices()
        return bounds
