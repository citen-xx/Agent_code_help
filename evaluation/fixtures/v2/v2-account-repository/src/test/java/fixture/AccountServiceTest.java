package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class AccountServiceTest {
    @Test
    public void withdrawalThenOverdraftValidation() {
        AccountService service = new AccountService(new AccountRepository());
        assertEquals(60, service.withdraw(40));
        try {
            service.withdraw(100);
            org.junit.Assert.fail("expected insufficient funds");
        } catch (InsufficientFundsException expected) {
            // expected
        }
    }
}
