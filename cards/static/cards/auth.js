for (const toggle of document.querySelectorAll('[data-password-toggle]')) {
    const password = document.getElementById(toggle.getAttribute('aria-controls'));
    toggle.hidden = false;
    toggle.addEventListener('click', () => {
        const showPassword = password.type === 'password';
        password.type = showPassword ? 'text' : 'password';
        toggle.textContent = showPassword ? '비밀번호 숨기기' : '비밀번호 보기';
    });
}
