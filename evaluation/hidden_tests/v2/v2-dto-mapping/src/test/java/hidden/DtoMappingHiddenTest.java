package hidden;

import fixture.User;
import fixture.UserDto;
import fixture.UserMapper;
import org.junit.Assert;
import org.junit.Test;

public class DtoMappingHiddenTest {
    @Test
    public void preservesEveryField() {
        UserDto dto = new UserMapper().toDto(new User(7, "Alice", "a@x"));
        Assert.assertEquals("Alice", dto.getName());
        Assert.assertEquals("a@x", dto.getEmail());
    }
}
