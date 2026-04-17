from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class SendVerificationCodeRequest(BaseModel):
    email: str = Field(min_length=3)


class VerifyEmailCodeRequest(BaseModel):
    email: str = Field(min_length=3)
    code: str = Field(min_length=4, max_length=10)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)


class SignUpRequest(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    password_confirm: str = Field(min_length=8)
    college_name: str = Field(min_length=1)
    department_name: str = Field(min_length=1)
    invitation_code: str | None = None

    @model_validator(mode="after")
    def validate_passwords_match(self) -> "SignUpRequest":
        if self.password != self.password_confirm:
            raise ValueError("비밀번호와 비밀번호 확인이 일치하지 않습니다.")
        return self