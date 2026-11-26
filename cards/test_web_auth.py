from secrets import token_urlsafe

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import resolve, reverse


User = get_user_model()


class SignUpTests(TestCase):
    def setUp(self):
        self.url = reverse('cards:signup')
        self.password = token_urlsafe(24)
        self.data = {
            'username': 'new_cardpicker',
            'password1': self.password,
            'password2': self.password,
        }

    def test_signup_redirects_to_login_without_authenticating(self):
        response = self.client.post(self.url, self.data)
        self.assertRedirects(response, reverse('cards:login'))
        self.assertTrue(User.objects.filter(username=self.data['username']).exists())
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_signup_stores_a_password_hash(self):
        self.client.post(self.url, self.data)
        user = User.objects.get(username=self.data['username'])
        self.assertTrue(user.check_password(self.password))
        self.assertTrue(user.password != self.password)

    def test_duplicate_usernames_are_rejected_including_case_variants(self):
        User.objects.create_user(username=self.data['username'])
        for username in (self.data['username'], self.data['username'].upper()):
            with self.subTest(username=username):
                response = self.client.post(self.url, {**self.data, 'username': username})
                error = response.context['form'].errors.as_data()['username'][0]
                self.assertEqual(error.code, 'unique')
        self.assertEqual(User.objects.count(), 1)

    def test_unicode_case_variant_is_rejected(self):
        User.objects.create_user(username='Ä카드')
        response = self.client.post(self.url, {**self.data, 'username': 'ä카드'})
        self.assertEqual(response.context['form'].errors.as_data()['username'][0].code, 'unique')
        self.assertEqual(User.objects.count(), 1)

    def test_mismatched_passwords_preserve_username_without_rendering_passwords(self):
        confirmation = token_urlsafe(24)
        response = self.client.post(self.url, {**self.data, 'password2': confirmation})
        error = response.context['form'].errors.as_data()['password2'][0]
        self.assertEqual(error.code, 'password_mismatch')
        self.assertContains(response, f'value="{self.data["username"]}"')
        self.assertFalse(self.password.encode() in response.content)
        self.assertFalse(confirmation.encode() in response.content)
        self.assertFalse(User.objects.exists())

    def test_password_validation_rejects_short_passwords(self):
        short_password = token_urlsafe(2)
        response = self.client.post(self.url, {
            **self.data, 'password1': short_password, 'password2': short_password,
        })
        errors = response.context['form'].errors.as_data()['password2']
        self.assertIn('password_too_short', [error.code for error in errors])
        self.assertFalse(User.objects.exists())

    def test_required_fields_and_korean_password_help_are_displayed(self):
        response = self.client.post(self.url, {})
        self.assertEqual(set(response.context['form'].errors), {'username', 'password1', 'password2'})
        self.assertContains(response, '비밀번호 확인')
        self.assertContains(response, '8자')
        self.assertFalse(User.objects.exists())

    def test_signup_success_message_is_displayed_on_login(self):
        response = self.client.post(self.url, self.data, follow=True)
        self.assertContains(response, '회원가입이 완료됐어요. 새 계정으로 로그인해 주세요.')


class AuthFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = token_urlsafe(24)
        cls.user = User.objects.create_user(username='cardpicker', password=cls.password)

    def setUp(self):
        self.home = reverse('cards:home')
        self.login = reverse('cards:login')
        self.logout = reverse('cards:logout')
        self.credentials = {'username': self.user.username, 'password': self.password}

    def test_login_page_displays_brand_css_and_anonymous_navigation(self):
        response = self.client.get(self.login)
        self.assertTemplateUsed(response, 'cards/login.html')
        self.assertContains(response, 'CardPick')
        self.assertContains(response, 'href="/static/cards/app.css"')
        self.assertContains(response, f'href="{reverse("cards:signup")}"')
        self.assertNotContains(response, '로그아웃')
        self.assertContains(response, 'CardPick은 가상 카드와 저장된 모의 거래로 혜택을 비교하는 학습용 서비스입니다.')

    def test_login_redirects_home_and_creates_session(self):
        response = self.client.post(self.login, self.credentials)
        self.assertRedirects(response, self.home)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))

    def test_wrong_password_and_unknown_username_have_the_same_error(self):
        wrong_credentials = (
            {**self.credentials, 'password': token_urlsafe(24)},
            {**self.credentials, 'username': 'unknown_user'},
        )
        for data in wrong_credentials:
            with self.subTest(username=data['username']):
                response = self.client.post(self.login, data)
                self.assertEqual(response.context['form'].non_field_errors(), [
                    '아이디 또는 비밀번호를 다시 확인해 주세요.',
                ])
                self.assertContains(response, '아이디 또는 비밀번호를 다시 확인해 주세요.')
                self.assertNotIn('_auth_user_id', self.client.session)

    def test_local_next_is_rendered_and_used_after_login(self):
        target = '/?from=login'
        response = self.client.get(self.login, {'next': target})
        self.assertContains(response, f'name="next" value="{target}"')
        response = self.client.post(self.login, {**self.credentials, 'next': target})
        self.assertRedirects(response, target)

    def test_external_next_is_removed_and_rejected_on_post(self):
        for target in ('https://external.example/', '//external.example/'):
            with self.subTest(target=target):
                response = self.client.get(self.login, {'next': target})
                self.assertNotContains(response, 'name="next"')
                response = self.client.post(self.login, {**self.credentials, 'next': target})
                self.assertRedirects(response, self.home)

    def test_post_logout_ends_session_and_redirects_to_login(self):
        self.client.force_login(self.user)
        response = self.client.post(self.logout, {'next': self.home})
        self.assertRedirects(response, self.login)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertRedirects(self.client.get(self.home), f'{self.login}?next={self.home}')

    def test_get_logout_keeps_session(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.logout).status_code, 405)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))

    def test_anonymous_home_redirects_to_login(self):
        self.assertRedirects(self.client.get(self.home), f'{self.login}?next={self.home}')

    def test_authenticated_home_displays_the_session_user_and_logout(self):
        self.client.force_login(self.user)
        response = self.client.get(self.home, {'user_id': 'other_user'})
        self.assertTemplateUsed(response, 'cards/home.html')
        self.assertContains(response, self.user.username)
        self.assertNotContains(response, 'other_user')
        self.assertContains(response, '나에게 맞는 카드 찾기')
        self.assertContains(response, f'action="{self.logout}" method="post"')
        self.assertNotContains(response, '회원가입')

    def test_auth_posts_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse('cards:signup'), {}).status_code, 403)
        self.assertEqual(client.post(self.login, self.credentials).status_code, 403)
        client.force_login(self.user)
        self.assertEqual(client.post(self.logout).status_code, 403)
        self.assertEqual(client.session['_auth_user_id'], str(self.user.pk))

    def test_admin_url_remains_available(self):
        self.assertEqual(reverse('admin:index'), '/admin/')
        self.assertEqual(resolve('/admin/').view_name, 'admin:index')
        self.assertEqual(self.client.get('/admin/').status_code, 302)
