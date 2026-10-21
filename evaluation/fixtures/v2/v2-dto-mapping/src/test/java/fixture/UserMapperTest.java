package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class UserMapperTest {
    @Test
    public void mapsAllFields() {
        UserDto dto = new UserMapper().toDto(new User(7, "Alice", "a@x"));
        assertEquals(7, dto.getId());
        assertEquals("a@x", dto.getEmail());
    }
}
