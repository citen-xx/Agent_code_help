package hidden;

import fixture.AccountRepository;
import fixture.AccountService;
import fixture.InsufficientFundsException;
import org.junit.Test;

public class AccountHiddenTest {
    @Test
    public void exactBalanceCanBeWithdrawn() {
        org.junit.Assert.assertEquals(
            0, new AccountService(new AccountRepository()).withdraw(100)
        );
    }

    @Test(expected = InsufficientFundsException.class)
    public void rejectsOverdraft() {
        new AccountService(new AccountRepository()).withdraw(101);
    }
}
