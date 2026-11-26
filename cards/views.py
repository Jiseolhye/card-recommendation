import logging
from itertools import groupby

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LogoutView
from django.db import DatabaseError, transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, ProtectedError, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .benefit_calculation import SEOUL, DiscountReason, month_bounds
from .benefit_service import current_user_spending, read_owned_card_summary
from .forms import AddCardForm, PaymentForm, SignUpForm, TransactionFilterForm
from .models import BenefitType, CardBenefit, Transaction, UserCard
from .recommendation_service import recommend_best_card


logger = logging.getLogger(__name__)
RECOMMENDATION_ERROR = '추천 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.'
DISCOUNT_MESSAGES = {
    DiscountReason.APPLIED: '결제 조건을 충족해 예상 할인이 적용돼요.',
    DiscountReason.NO_MATCHING_BENEFIT: '선택한 업종의 할인 혜택이 없어요.',
    DiscountReason.PREVIOUS_SPENDING_NOT_MET: '지난달 이용금액이 혜택 기준에 미치지 않아요.',
    DiscountReason.MINIMUM_AMOUNT_NOT_MET: '이번 결제금액이 최소 결제 조건보다 낮아요.',
    DiscountReason.MONTHLY_LIMIT_EXHAUSTED: '이번 달 예상 할인 한도를 모두 사용했어요.',
    DiscountReason.PER_TRANSACTION_LIMIT_ZERO: '이 결제에 적용할 수 있는 건별 할인 한도가 없어요.',
    DiscountReason.ROUNDED_TO_ZERO: '원 단위로 계산하면 예상 할인액이 0원이에요.',
}


def _won(amount):
    return f'{amount:,}원'


def _condition_status(threshold, amount):
    if threshold is None:
        return '해당 없음'
    if threshold == 0:
        return '조건 없음'
    return '충족' if amount >= threshold else '미충족'


def _comparison_rows(recommendation, *, amount, category):
    benefits = {
        benefit.card_id: benefit for benefit in CardBenefit.objects.filter(
            card_id__in=[card.card_id for card in recommendation.comparisons],
            category=category, benefit_type=BenefitType.DISCOUNT,
        )
    }
    rows = []
    for rank, card in enumerate(recommendation.comparisons, start=1):
        estimate = card.estimate
        benefit = benefits.get(card.card_id)
        tied_first = recommendation.tied_for_first and estimate.estimated_discount == recommendation.estimated_discount
        rows.append({
            'card': card,
            'rank': '공동 1위' if tied_first else f'{rank}위',
            'discount': _won(estimate.estimated_discount),
            'reason': DISCOUNT_MESSAGES[estimate.reason],
            'spending_status': _condition_status(
                benefit.required_spending if benefit else None, estimate.previous_month_spending,
            ),
            'minimum_status': _condition_status(benefit.minimum_amount if benefit else None, amount),
        })
    return rows


@login_required
@require_http_methods(['GET', 'POST'])
def home(request):
    as_of = timezone.now()
    form = PaymentForm(request.POST if request.method == 'POST' else None)
    context = {'form': form, 'submitted': request.method == 'POST'}
    try:
        context['card_count'] = request.user.user_cards.count()
        context['monthly_spending'] = _won(current_user_spending(user_id=request.user.pk, as_of=as_of))
        if context['submitted'] and form.is_valid():
            amount = form.cleaned_data['amount']
            category = form.cleaned_data['category']
            recommendation = recommend_best_card(
                user_id=request.user.pk, amount=amount, category=category, as_of=as_of,
            )
            comparisons = _comparison_rows(recommendation, amount=amount, category=category)
            context.update(recommendation=recommendation, comparisons=comparisons)
            if recommendation.recommended_card is not None:
                estimate = recommendation.recommended_card.estimate
                context.update(
                    estimated_discount=_won(recommendation.estimated_discount),
                    estimated_payment=_won(amount - recommendation.estimated_discount),
                    recommendation_reason=DISCOUNT_MESSAGES[estimate.reason],
                    additional_savings=(None if recommendation.additional_savings is None
                                        else _won(recommendation.additional_savings)),
                )
    except (DatabaseError, UserCard.DoesNotExist):
        status = 503
    except Exception as error:
        logger.error('Unexpected failure while preparing the recommendation dashboard (%s)', type(error).__name__)
        status = 500
    else:
        status = 200
    if status != 200:
        context['recommendation_error'] = RECOMMENDATION_ERROR
    return render(request, 'cards/home.html', context, status=status)


def _render_cards(request, form):
    cards = request.user.user_cards.select_related('card').prefetch_related(
        Prefetch('card__benefits', queryset=CardBenefit.objects.filter(
            benefit_type=BenefitType.DISCOUNT,
        ).order_by('category')),
    ).annotate(has_transactions=Exists(Transaction.objects.filter(user_card_id=OuterRef('pk'))))
    owned_cards = list(cards.order_by('card__issuer', 'card__name', 'pk'))
    return render(request, 'cards/card_list.html', {
        'owned_cards': owned_cards, 'form': form,
        'can_add_card': form.fields['card'].queryset.exists(),
    })


@login_required
@require_GET
def card_list(request):
    return _render_cards(request, AddCardForm(user=request.user))


@login_required
@require_POST
def card_add(request):
    form = AddCardForm(request.POST, user=request.user)
    if form.is_valid():
        # get_or_create uses the database constraint if another request registers first.
        _, created = request.user.user_cards.get_or_create(card=form.cleaned_data['card'])
        if created:
            messages.success(request, '내 카드에 등록했어요.')
            return redirect('cards:card_list')
        form.add_error('card', '이미 등록한 카드예요.')
    return _render_cards(request, form)


@login_required
@require_GET
def card_detail(request, pk):
    try:
        summary = read_owned_card_summary(user_id=request.user.pk, user_card_id=pk, as_of=timezone.now())
    except UserCard.DoesNotExist:
        raise Http404 from None

    benefits = []
    for usage in summary.benefits:
        benefit = usage.benefit
        benefits.append({
            'category': benefit.get_category_display(),
            'rate': benefit.benefit_rate,
            'required_spending': _won(benefit.required_spending) if benefit.required_spending else '조건 없음',
            'minimum_amount': _won(benefit.minimum_amount) if benefit.minimum_amount else '조건 없음',
            'per_transaction_limit': '한도 없음' if benefit.per_transaction_limit is None else _won(benefit.per_transaction_limit),
            'monthly_limit': '한도 없음' if benefit.monthly_limit is None else _won(benefit.monthly_limit),
            'estimated_used': '추정 대상 아님' if usage.estimated_used is None else _won(usage.estimated_used),
            'remaining': '한도 없음' if usage.remaining is None else _won(usage.remaining),
        })
    return render(request, 'cards/card_detail.html', {
        'card': summary.user_card.card,
        'user_card_id': summary.user_card.pk,
        'previous_spending': _won(summary.previous_month_spending),
        'monthly_spending': _won(summary.current_month_spending),
        'benefits': benefits,
    })


@login_required
@require_POST
def card_delete(request, pk):
    user_card = get_object_or_404(request.user.user_cards, pk=pk)
    try:
        user_card.delete()
    except ProtectedError:
        messages.error(request, '이용내역이 연결된 카드는 삭제할 수 없어요.')
    else:
        messages.success(request, '내 카드에서 삭제했어요.')
    return redirect('cards:card_list')


def _transaction_results(*, history, bounds, owned_cards, selected_card):
    month_rows = history.filter(
        transaction_date__gte=bounds.current_start, transaction_date__lt=bounds.next_start,
    )
    totals = {
        row['user_card_id']: (row['total'], row['count'])
        for row in month_rows.values('user_card_id').annotate(total=Sum('amount'), count=Count('pk'))
    }
    month_total = sum(total for total, _ in totals.values())
    selected_total, selected_count = month_total, sum(count for _, count in totals.values())
    if selected_card is not None:
        month_rows = month_rows.filter(user_card_id=selected_card.pk)
        selected_total, selected_count = totals.get(selected_card.pk, (0, 0))

    card_totals = []
    for owned in owned_cards:
        amount = totals.get(owned.pk, (0, 0))[0]
        card_totals.append({
            'id': owned.pk, 'name': owned.card.name, 'issuer': owned.card.issuer,
            'amount': amount, 'formatted_amount': _won(amount),
            'share': f'{100 * amount / month_total:.1f}'.rstrip('0').rstrip('.') if month_total else '0',
        })
    card_totals.sort(key=lambda row: (-row['amount'], row['id']))

    rows = month_rows.select_related('user_card__card').order_by('-transaction_date', '-pk')
    groups = [
        {'date': day, 'entries': [{'payment': row, 'amount': _won(row.amount)} for row in payments]}
        for day, payments in groupby(rows, key=lambda row: timezone.localtime(row.transaction_date, SEOUL).date())
    ]
    return {
        'selected_month': timezone.localtime(bounds.current_start, SEOUL).date(),
        'selected_card': selected_card,
        'selected_total': _won(selected_total), 'selected_count': selected_count,
        'month_total': month_total, 'formatted_month_total': _won(month_total),
        'card_totals': card_totals, 'transaction_groups': groups,
    }


@login_required
@require_GET
def transaction_list(request):
    as_of = timezone.now()
    current_month = timezone.localtime(as_of, SEOUL).date().replace(day=1)
    current_start = month_bounds(as_of).current_start
    try:
        with transaction.atomic():
            history = Transaction.objects.filter(user_card__user_id=request.user.pk, transaction_date__lt=as_of)
            months = [month.date() for month in history.datetimes('transaction_date', 'month', 'DESC', tzinfo=SEOUL)]
            owned_cards = request.user.user_cards.select_related('card').order_by('card__name', 'pk')
            data = {'month': current_month.isoformat()[:7], **request.GET.dict()}
            form = TransactionFilterForm(data, current_month=current_month, past_months=months, owned_cards=owned_cards)
            context = {
                'form': form, 'current_month': current_month, 'has_history': bool(months),
                'current_total': _won(current_user_spending(user_id=request.user.pk, as_of=as_of)),
                'current_count': history.filter(transaction_date__gte=current_start).count(),
                'has_cards': bool(owned_cards),
            }
            valid = form.is_valid()
            if valid:
                context.update(_transaction_results(
                    history=history, bounds=form.cleaned_data['month'],
                    owned_cards=owned_cards, selected_card=form.cleaned_data['card'],
                ))
    except DatabaseError:
        return render(request, 'cards/transaction_list.html', {'load_failed': True}, status=503)
    return render(request, 'cards/transaction_list.html', context, status=200 if valid else 400)


@sensitive_post_parameters('password1', 'password2')
@require_http_methods(['GET', 'POST'])
def signup(request):
    if request.user.is_authenticated:
        return redirect('cards:home')

    form = SignUpForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, '회원가입이 완료됐어요. 새 계정으로 로그인해 주세요.')
        return redirect('cards:login')

    return render(request, 'cards/signup.html', {'form': form})


class CardPickLogoutView(LogoutView):
    def get_success_url(self):
        return self.get_default_redirect_url()
