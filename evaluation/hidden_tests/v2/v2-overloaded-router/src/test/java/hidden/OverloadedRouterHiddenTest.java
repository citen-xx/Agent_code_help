package hidden;

import fixture.NotificationService;
import org.junit.Assert;
import org.junit.Test;

public class OverloadedRouterHiddenTest {
    @Test
    public void textOverloadRemainsText() {
        Assert.assertEquals("text:hello", new NotificationService().notifyText("hello"));
    }
}
